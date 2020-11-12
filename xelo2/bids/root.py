from json import dump, load
from pathlib import Path
from copy import copy as c
from collections import defaultdict
from logging import getLogger
from datetime import date, datetime
from shutil import copy, rmtree

from bidso.utils import replace_extension
from PyQt5.QtGui import QGuiApplication
from PyQt5.QtSql import QSqlQuery

from ..api import list_subjects, Run
from .mri import convert_mri
from .ieeg import convert_ieeg
from .physio import convert_physio
from .events import convert_events
from ..io.export_db import prepare_query
from .utils import rename_task
from .templates import (
    JSON_PARTICIPANTS,
    JSON_SESSIONS,
    )

# protocols
PROTOCOL_HEALTHY = [
    '16-816',
    ]


lg = getLogger(__name__)


def prepare_subset(db, where, subset=None):

    query_str = prepare_query(('subjects', 'sessions', 'runs', 'recordings'))[0]
    query = QSqlQuery(db)
    query.prepare(f"""{query_str} WHERE {where}""")
    if not query.exec():
        raise SyntaxError(query.lastError().text())

    if subset is None:
        subset = {'subjects': [], 'sessions': [], 'runs': []}

    while query.next():
        subset['subjects'].append(query.value('subjects.id'))
        subset['sessions'].append(query.value('sessions.id'))
        subset['runs'].append(query.value('runs.id'))

    return subset


def create_bids(db, data_path, deface=True, subset=None, progress=None):

    if subset is not None:
        subset = add_intended_for(db, subset)

        subset_subj = set(subset['subjects'])
        subset_sess = set(subset['sessions'])
        subset_run = set(subset['runs'])

    data_path = Path(data_path)
    if data_path.exists():
        rmtree(data_path, ignore_errors=True)
    data_path.mkdir(parents=True, exist_ok=True)

    # the dataset_description.json is used by find_root, in some subscripts
    _make_dataset_description(data_path)

    intendedfor = {}

    i = 0
    participants = []
    for subj in list_subjects(db):
        bids_name = {
            'sub': None,
            'ses': None,
            'task': None,
            'acq': None,
            'rec': None,
            'dir': None,
            'run': None,
            'rec': None,
            }
        if subset is not None and subj.id not in subset_subj:
            continue

        # use relative date based on date_of_signature
        reference_dates = [p.date_of_signature for p in subj.list_protocols()]
        if len(reference_dates) == 0:
            lg.warning(f'You need to add at least one research protocol for {subj.codes}')
            reference_dates = [datetime.now().date(), ]

        reference_date = max(reference_dates)
        if reference_date is None:
            lg.warning(f'You need to add date_of_signature to the METC of {subj.codes}')
            lg.info('Using date of the first task performed by the subject')
            reference_date = min([x.start_time for x in subj.list_sessions()]).date()

        lg.info(f'Adding {subj.codes}')
        codes = subj.codes
        if len(codes) == 0:
            code = 'id{subj.id}'  # use id if code is empty
        else:
            code = codes[0]
        bids_name['sub'] = 'sub-' + code
        subj_path = data_path / bids_name['sub']
        subj_path.mkdir(parents=True, exist_ok=True)

        if subj.date_of_birth is None:
            lg.warning(f'You need to add date_of_birth to {subj.codes}')
            age = 'n/a'
        else:
            age = (reference_date - subj.date_of_birth).days // 365.2425
            age = f'{age:.0f}'

        patient_or_healthy = 'patient'
        for p in subj.list_protocols():
            if p.metc in PROTOCOL_HEALTHY:
                patient_or_healthy = 'healthy'

        participants.append({
            'participant_id': bids_name['sub'],
            'sex': subj.sex,
            'age': age,
            'group': patient_or_healthy,
            })

        sess_count = defaultdict(int)
        sess_files = []
        for sess in subj.list_sessions():
            sess_count[_make_sess_name(sess)] += 1  # also count the sessions which are not included
            if subset is not None and sess.id not in subset_sess:
                continue

            bids_name['ses'] = f'ses-{_make_sess_name(sess)}{sess_count[_make_sess_name(sess)]}'
            sess_path = subj_path / bids_name['ses']
            sess_path.mkdir(parents=True, exist_ok=True)
            lg.info(f'Adding {bids_name["sub"]} / {bids_name["ses"]}')

            sess_files.append({
                'session_id': bids_name['ses'],
                'resection': 'n/a',
                'implantation': 'no',
                'breathing_challenge': 'no',
                })
            if sess.name in ('IEMU', 'OR', 'CT'):
                sess_files[-1]['implantation'] = 'yes'

            run_count = defaultdict(int)
            run_files = []
            for run in sess.list_runs():
                run_count[run.task_name] += 1  # also count the runs which are not included

                if subset is not None and run.id not in subset_run:
                    continue

                if len(run.list_recordings()) == 0:
                    lg.warning(f'No recordings for {subj.codes}/{run.task_name}')
                    continue

                if progress is not None:
                    progress.setValue(i)
                    i += 1
                    progress.setLabelText(f'Exporting "{subj.codes}" / "{sess.name}" / "{run.task_name}"')
                    QGuiApplication.processEvents()

                    if progress.wasCanceled():
                        return

                acquisition = get_bids_acquisition(run)
                bids_name['run'] = f'run-{run_count[run.task_name]}'

                if acquisition in ('ieeg', 'func'):
                    bids_name['task'] = f'task-{rename_task(run.task_name)}'
                else:
                    bids_name['task'] = None
                mod_path = sess_path / acquisition
                mod_path.mkdir(parents=True, exist_ok=True)
                lg.info(f'Adding {bids_name["sub"]} / {bids_name["ses"]} / {acquisition} / {bids_name["task"]} / {bids_name["run"]}')

                data_name = None
                for rec in run.list_recordings():

                    if rec.modality in ('bold', 'T1w', 'T2w', 'T2star', 'PD', 'FLAIR', 'angio', 'epi'):

                        if rec.PhaseEncodingDirection is not None:
                            bids_name['dir'] = 'dir-' + rec.PhaseEncodingDirection
                        else:
                            bids_name['dir'] = None

                        data_name = convert_mri(run, rec, mod_path, c(bids_name), deface)

                    elif rec.modality == 'ieeg':
                        if run.duration is None:
                            lg.warning(f'You need to specify duration for {subj.codes}/{run.task_name}')
                            continue
                        data_name = convert_ieeg(run, rec, mod_path, c(bids_name), intendedfor)

                    elif rec.modality == 'physio':
                        if data_name is None:
                            lg.warning('physio only works after another recording modality')
                        else:
                            convert_physio(rec, mod_path, c(bids_name))

                    else:
                        lg.warning(f'Unknown modality {rec.modality} for {rec}')
                        continue

                    if data_name is not None and acquisition in ('ieeg', 'func'):
                        convert_events(run, mod_path, c(bids_name))

                    if data_name is not None and rec.modality != 'physio':  # secondary modality
                        intendedfor[run.id] = data_name
                        run_files.append({
                            'filename': data_name,
                            'acq_time': _set_date_to_1900(reference_date, run.start_time).isoformat(timespec='seconds'),
                            })

            if len(run_files) == 0:
                continue
            tsv_file = sess_path / f'{bids_name["sub"]}_{bids_name["ses"]}_scans.tsv'
            if run_files:
                _list_scans(tsv_file, c(run_files), sess_path)

        tsv_file = subj_path / f'{bids_name["sub"]}_sessions.tsv'
        if sess_files:
            _list_scans(tsv_file, sess_files, data_path)

        json_sessions = tsv_file.with_suffix('.json')
        copy(JSON_SESSIONS, json_sessions)  # https://github.com/bids-standard/bids-validator/issues/888

    # add IntendedFor for top_up scans
    _add_intendedfor(db, data_path, intendedfor)

    # remove phase because we get lots of warnings from BIDS
    remove_phase(data_path)

    # here the rest
    _make_README(data_path)
    tsv_file = data_path / 'participants.tsv'
    _list_scans(tsv_file, participants, data_path)
    json_participants = tsv_file.with_suffix('.json')
    copy(JSON_PARTICIPANTS, json_participants)


def _list_scans(tsv_file, scans, root_dir):
    """
    Parameters
    ----------
    """
    if 'filename' in scans[0]:
        for scan in scans:
            scan['filename'] = str(scan['filename'].relative_to(root_dir))

    with tsv_file.open('w') as f:
        f.write('\t'.join(scans[0].keys()) + '\n')
        for scan in scans:
            for k, v in scan.items():
                if v is None:
                    scan[k] = 'n/a'
            f.write('\t'.join(scan.values()) + '\n')


def _make_dataset_description(data_path):
    """Generate general description of the dataset

    Parameters
    ----------
    data_path : Path
        root BIDS directory
    """

    d = {
        "Name": data_path.name,
        "BIDSVersion": "1.2.1",
        "License": "CCBY",
        "Authors": [
            "Giovanni Piantoni",
            "Nick Ramsey",
            "Natalia Petridou",
            ],
        "Acknowledgements": "",
        "HowToAcknowledge": '',
        "Funding": [
            "NIH R01 MH111417",
            ],
        "ReferencesAndLinks": ["", ],
        "DatasetDOI": ""
        }

    with (data_path / 'dataset_description.json').open('w') as f:
        dump(d, f, ensure_ascii=False, indent=' ')


def get_bids_acquisition(run):
    for recording in run.list_recordings():
        modality = recording.modality
        if modality == 'ieeg':
            return 'ieeg'
        elif modality in ('T1w', 'T2w', 'T2star', 'FLAIR', 'PD', 'angio'):
            return 'anat'
        elif modality in ('bold', 'phase'):
            return 'func'
        elif modality in ('epi', ):
            return 'fmap'
        elif modality in ('ct', ):
            return 'ct'

    raise ValueError(f'I cannot determine BIDS folder for {repr(run)}')


def add_intended_for(db, subset):
    run_t1w = add_intended_for_elec(db, subset)
    run_topup = add_intended_for_topup(db, subset)

    intendedfor = run_t1w + run_topup

    if len(intendedfor) == 0:
        return subset
    else:
        intendedfor_str = ', '.join(str(x) for x in intendedfor)
        run_id_sql = f'`runs`.`id` in ({intendedfor_str})'
        return prepare_subset(db, run_id_sql, subset=subset)


def add_intended_for_topup(db, subset):
    """Add topup"""
    topups = []
    for run_id in subset['runs']:
        query = QSqlQuery(db)
        query.prepare("SELECT run_id FROM intended_for WHERE target = :targetid")
        query.bindValue(':targetid', run_id)

        if not query.exec():
            raise SyntaxError(query.lastError().text())

        while query.next():
            topups.append(query.value('run_id'))

    return topups


def add_intended_for_elec(db, subset):
    """Electrodes also need the reference T1w images, so we add it here"""

    reference_t1w = []
    for run_id in subset['runs']:
        run = Run(db, id=run_id)
        for rec in run.list_recordings():
            electrodes = rec.electrodes
            if electrodes is not None:
                t1w_id = electrodes.IntendedFor
                if t1w_id is not None:
                    reference_t1w.append(t1w_id)

    return reference_t1w


def _make_README(data_path):

    with (data_path / 'README').open('w') as f:
        f.write('Converted with xelo2')

    # this is necessary to work around this issue
    # https://github.com/bids-standard/bids-specification/issues/209
    with (data_path / '.bidsignore').open('w') as f:
        f.write('sub-*/ses-*/ieeg/*_physio.tsv.gz\n')
        f.write('sub-*/ses-*/ieeg/*_physio.json\n')


def _set_date_to_1900(base_date, datetime_of_interest):
    if datetime_of_interest is None:  # run.start_time is null
        return datetime(1900, 1, 1, 0, 0, 0)
    else:
        return datetime.combine(
            date(1900, 1, 1) + (datetime_of_interest.date() - base_date),
            datetime_of_interest.time())


def _make_sess_name(sess):
    if sess.name == 'MRI':
        MagneticFieldStrength = sess.MagneticFieldStrength
        if MagneticFieldStrength is None:
            lg.warning(f'Please specify Magnetic Field Strength for {sess}')
            sess_name = 'mri'
        elif MagneticFieldStrength == '1.5T':  # we cannot use 1.5 in session name
            sess_name = 'mri'
        else:
            sess_name = MagneticFieldStrength.lower()
    else:
        sess_name = sess.name.lower()
    return sess_name


def _add_intendedfor(db, bids_dir, intendedfor):
    for run_id, relative_path in intendedfor.items():
        targets = find_intendedfor(db, run_id)  # find all the targets
        targets = set(targets) & set(intendedfor)  # only targets in this dataset
        if len(targets) == 0:
            continue

        fields = []
        for target_id in targets:
            target_file = intendedfor[target_id]
            # remove sub- from the path (note the inconsistency between fieldmaps and T1w/elec)
            target_file = target_file.partition('/')[-1]
            fields.append(target_file)

        json_file = replace_extension(bids_dir / relative_path, '.json')
        _add_intendedfor_to_json(json_file, fields)


def _add_intendedfor_to_json(json_file, fields):
    if json_file.exists():
        with json_file.open() as f:
            sidecar = load(f)
    else:
        lg.warning('Adding IntendedFor to {json_file}, but this file does not exist')
        sidecar = {}

    sidecar['IntendedFor'] = fields

    with json_file.open('w') as f:
        dump(sidecar, f, indent=2)


def find_intendedfor(db, run_id):
    query = QSqlQuery(db)
    query.prepare("SELECT target FROM intended_for WHERE run_id = :runid")
    query.bindValue(':runid', run_id)

    if not query.exec():
        raise SyntaxError(query.lastError().text())

    topups = []
    while query.next():
        topups.append(query.value('target'))
    return topups


def remove_phase(bids_dir):
    """I cannot specify phase.json so we get lots of errors when including phase.nii.gz
    https://github.com/bids-standard/bids-validator/issues/1074
    """
    for phase in bids_dir.rglob('*_phase.nii.gz'):
        phase.unlink()
