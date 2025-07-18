"""The database for managed jobs status."""
# TODO(zhwu): maybe use file based status instead of database, so
# that we can easily switch to a s3-based storage.
import enum
import functools
import json
import pathlib
import sqlite3
import threading
import time
import typing
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import colorama

from sky import exceptions
from sky import sky_logging
from sky.skylet import constants
from sky.utils import common_utils
from sky.utils import db_utils

if typing.TYPE_CHECKING:
    import sky

CallbackType = Callable[[str], None]

logger = sky_logging.init_logger(__name__)


# === Database schema ===
# `spot` table contains all the finest-grained tasks, including all the
# tasks of a managed job (called spot for legacy reason, as it is generalized
# from the previous managed spot jobs). All tasks of the same job will have the
# same `spot_job_id`.
# The `job_name` column is now deprecated. It now holds the task's name, i.e.,
# the same content as the `task_name` column.
# The `job_id` is now not really a job id, but a only a unique
# identifier/primary key for all the tasks. We will use `spot_job_id`
# to identify the job.
# TODO(zhwu): schema migration may be needed.
def create_table(cursor, conn):
    # Enable WAL mode to avoid locking issues.
    # See: issue #3863, #1441 and PR #1509
    # https://github.com/microsoft/WSL/issues/2395
    # TODO(romilb): We do not enable WAL for WSL because of known issue in WSL.
    #  This may cause the database locked problem from WSL issue #1441.
    if not common_utils.is_wsl():
        try:
            cursor.execute('PRAGMA journal_mode=WAL')
        except sqlite3.OperationalError as e:
            if 'database is locked' not in str(e):
                raise
            # If the database is locked, it is OK to continue, as the WAL mode
            # is not critical and is likely to be enabled by other processes.

    cursor.execute("""\
        CREATE TABLE IF NOT EXISTS spot (
        job_id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_name TEXT,
        resources TEXT,
        submitted_at FLOAT,
        status TEXT,
        run_timestamp TEXT CANDIDATE KEY,
        start_at FLOAT DEFAULT NULL,
        end_at FLOAT DEFAULT NULL,
        last_recovered_at FLOAT DEFAULT -1,
        recovery_count INTEGER DEFAULT 0,
        job_duration FLOAT DEFAULT 0,
        failure_reason TEXT,
        spot_job_id INTEGER,
        task_id INTEGER DEFAULT 0,
        task_name TEXT,
        specs TEXT,
        local_log_file TEXT DEFAULT NULL)""")
    conn.commit()

    db_utils.add_column_to_table(cursor, conn, 'spot', 'failure_reason', 'TEXT')
    # Create a new column `spot_job_id`, which is the same for tasks of the
    # same managed job.
    # The original `job_id` no longer has an actual meaning, but only a legacy
    # identifier for all tasks in database.
    db_utils.add_column_to_table(cursor,
                                 conn,
                                 'spot',
                                 'spot_job_id',
                                 'INTEGER',
                                 copy_from='job_id')
    db_utils.add_column_to_table(cursor,
                                 conn,
                                 'spot',
                                 'task_id',
                                 'INTEGER DEFAULT 0',
                                 value_to_replace_existing_entries=0)
    db_utils.add_column_to_table(cursor,
                                 conn,
                                 'spot',
                                 'task_name',
                                 'TEXT',
                                 copy_from='job_name')

    # Specs is some useful information about the task, e.g., the
    # max_restarts_on_errors value. It is stored in JSON format.
    db_utils.add_column_to_table(cursor,
                                 conn,
                                 'spot',
                                 'specs',
                                 'TEXT',
                                 value_to_replace_existing_entries=json.dumps({
                                     'max_restarts_on_errors': 0,
                                 }))
    db_utils.add_column_to_table(cursor, conn, 'spot', 'local_log_file',
                                 'TEXT DEFAULT NULL')

    # `job_info` contains the mapping from job_id to the job_name, as well as
    # information used by the scheduler.
    cursor.execute(f"""\
        CREATE TABLE IF NOT EXISTS job_info (
        spot_job_id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        schedule_state TEXT,
        controller_pid INTEGER DEFAULT NULL,
        dag_yaml_path TEXT,
        env_file_path TEXT,
        user_hash TEXT,
        workspace TEXT DEFAULT NULL,
        priority INTEGER DEFAULT {constants.DEFAULT_PRIORITY},
        entrypoint TEXT DEFAULT NULL,
        original_user_yaml_path TEXT DEFAULT NULL)""")

    db_utils.add_column_to_table(cursor, conn, 'job_info', 'schedule_state',
                                 'TEXT')

    db_utils.add_column_to_table(cursor, conn, 'job_info', 'controller_pid',
                                 'INTEGER DEFAULT NULL')

    db_utils.add_column_to_table(cursor, conn, 'job_info', 'dag_yaml_path',
                                 'TEXT')

    db_utils.add_column_to_table(cursor, conn, 'job_info', 'env_file_path',
                                 'TEXT')

    db_utils.add_column_to_table(cursor, conn, 'job_info', 'user_hash', 'TEXT')

    db_utils.add_column_to_table(cursor,
                                 conn,
                                 'job_info',
                                 'workspace',
                                 'TEXT DEFAULT NULL',
                                 value_to_replace_existing_entries='default')

    db_utils.add_column_to_table(
        cursor,
        conn,
        'job_info',
        'priority',
        'INTEGER',
        value_to_replace_existing_entries=constants.DEFAULT_PRIORITY)

    db_utils.add_column_to_table(cursor, conn, 'job_info', 'entrypoint', 'TEXT')
    db_utils.add_column_to_table(cursor, conn, 'job_info',
                                 'original_user_yaml_path', 'TEXT')
    conn.commit()


# Module-level connection/cursor; thread-safe as the db is initialized once
# across all threads.
def _get_db_path() -> str:
    """Workaround to collapse multi-step Path ops for type checker.
    Ensures _DB_PATH is str, avoiding Union[Path, str] inference.
    """
    path = pathlib.Path('~/.sky/spot_jobs.db')
    path = path.expanduser().absolute()
    path.parents[0].mkdir(parents=True, exist_ok=True)
    return str(path)


_DB_PATH = None
_db_init_lock = threading.Lock()


def _init_db(func):
    """Initialize the database."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        global _DB_PATH
        if _DB_PATH is not None:
            return func(*args, **kwargs)
        with _db_init_lock:
            if _DB_PATH is None:
                _DB_PATH = _get_db_path()
                db_utils.SQLiteConn(_DB_PATH, create_table)
        return func(*args, **kwargs)

    return wrapper


# job_duration is the time a job actually runs (including the
# setup duration) before last_recover, excluding the provision
# and recovery time.
# If the job is not finished:
# total_job_duration = now() - last_recovered_at + job_duration
# If the job is not finished:
# total_job_duration = end_at - last_recovered_at + job_duration
#
# Column names to be used in the jobs dict returned to the caller,
# e.g., via sky jobs queue. These may not correspond to actual
# column names in the DB and it corresponds to the combined view
# by joining the spot and job_info tables.
columns = [
    '_job_id',
    '_task_name',
    'resources',
    'submitted_at',
    'status',
    'run_timestamp',
    'start_at',
    'end_at',
    'last_recovered_at',
    'recovery_count',
    'job_duration',
    'failure_reason',
    'job_id',
    'task_id',
    'task_name',
    'specs',
    'local_log_file',
    # columns from the job_info table
    '_job_info_job_id',  # This should be the same as job_id
    'job_name',
    'schedule_state',
    'controller_pid',
    'dag_yaml_path',
    'env_file_path',
    'user_hash',
    'workspace',
    'priority',
    'entrypoint',
    'original_user_yaml_path',
]


class ManagedJobStatus(enum.Enum):
    """Managed job status, designed to be in serverless style.

    The ManagedJobStatus is a higher level status than the JobStatus.
    Each managed job submitted to a cluster will have a JobStatus
    on that cluster:
        JobStatus = [INIT, SETTING_UP, PENDING, RUNNING, ...]
    Whenever the cluster is preempted and recovered, the JobStatus
    will go through the statuses above again.
    That means during the lifetime of a managed job, its JobsStatus could be
    reset to INIT or SETTING_UP multiple times (depending on the preemptions).

    However, a managed job only has one ManagedJobStatus on the jobs controller.
        ManagedJobStatus = [PENDING, STARTING, RUNNING, ...]
    Mapping from JobStatus to ManagedJobStatus:
        INIT            ->  STARTING/RECOVERING
        SETTING_UP      ->  RUNNING
        PENDING         ->  RUNNING
        RUNNING         ->  RUNNING
        SUCCEEDED       ->  SUCCEEDED
        FAILED          ->  FAILED
        FAILED_SETUP    ->  FAILED_SETUP
    Not all statuses are in this list, since some ManagedJobStatuses are only
    possible while the cluster is INIT/STOPPED/not yet UP.
    Note that the JobStatus will not be stuck in PENDING, because each cluster
    is dedicated to a managed job, i.e. there should always be enough resource
    to run the job and the job will be immediately transitioned to RUNNING.

    You can see a state diagram for ManagedJobStatus in sky/jobs/README.md.
    """
    # PENDING: Waiting for the jobs controller to have a slot to run the
    # controller process.
    PENDING = 'PENDING'
    # SUBMITTED: This state used to be briefly set before immediately changing
    # to STARTING. Its use was removed in #5682. We keep it for backwards
    # compatibility, so we can still parse old jobs databases that may have jobs
    # in this state.
    # TODO(cooperc): remove this in v0.12.0
    DEPRECATED_SUBMITTED = 'SUBMITTED'
    # The submitted_at timestamp of the managed job in the 'spot' table will be
    # set to the time when the job controller begins running.
    # STARTING: The controller process is launching the cluster for the managed
    # job.
    STARTING = 'STARTING'
    # RUNNING: The job is submitted to the cluster, and is setting up or
    # running.
    # The start_at timestamp of the managed job in the 'spot' table will be set
    # to the time when the job is firstly transitioned to RUNNING.
    RUNNING = 'RUNNING'
    # RECOVERING: The cluster is preempted, and the controller process is
    # recovering the cluster (relaunching/failover).
    RECOVERING = 'RECOVERING'
    # CANCELLING: The job is requested to be cancelled by the user, and the
    # controller is cleaning up the cluster.
    CANCELLING = 'CANCELLING'
    # Terminal statuses
    # SUCCEEDED: The job is finished successfully.
    SUCCEEDED = 'SUCCEEDED'
    # CANCELLED: The job is cancelled by the user. When the managed job is in
    # CANCELLED status, the cluster has been cleaned up.
    CANCELLED = 'CANCELLED'
    # FAILED: The job is finished with failure from the user's program.
    FAILED = 'FAILED'
    # FAILED_SETUP: The job is finished with failure from the user's setup
    # script.
    FAILED_SETUP = 'FAILED_SETUP'
    # FAILED_PRECHECKS: the underlying `sky.launch` fails due to precheck
    # errors only. I.e., none of the failover exceptions, if any, is due to
    # resources unavailability. This exception includes the following cases:
    # 1. The optimizer cannot find a feasible solution.
    # 2. Precheck errors: invalid cluster name, failure in getting cloud user
    #    identity, or unsupported feature.
    FAILED_PRECHECKS = 'FAILED_PRECHECKS'
    # FAILED_NO_RESOURCE: The job is finished with failure because there is no
    # resource available in the cloud provider(s) to launch the cluster.
    FAILED_NO_RESOURCE = 'FAILED_NO_RESOURCE'
    # FAILED_CONTROLLER: The job is finished with failure because of unexpected
    # error in the controller process.
    FAILED_CONTROLLER = 'FAILED_CONTROLLER'

    def is_terminal(self) -> bool:
        return self in self.terminal_statuses()

    def is_failed(self) -> bool:
        return self in self.failure_statuses()

    def colored_str(self) -> str:
        color = _SPOT_STATUS_TO_COLOR[self]
        return f'{color}{self.value}{colorama.Style.RESET_ALL}'

    def __lt__(self, other) -> bool:
        status_list = list(ManagedJobStatus)
        return status_list.index(self) < status_list.index(other)

    @classmethod
    def terminal_statuses(cls) -> List['ManagedJobStatus']:
        return [
            cls.SUCCEEDED,
            cls.FAILED,
            cls.FAILED_SETUP,
            cls.FAILED_PRECHECKS,
            cls.FAILED_NO_RESOURCE,
            cls.FAILED_CONTROLLER,
            cls.CANCELLED,
        ]

    @classmethod
    def failure_statuses(cls) -> List['ManagedJobStatus']:
        return [
            cls.FAILED, cls.FAILED_SETUP, cls.FAILED_PRECHECKS,
            cls.FAILED_NO_RESOURCE, cls.FAILED_CONTROLLER
        ]

    @classmethod
    def processing_statuses(cls) -> List['ManagedJobStatus']:
        # Any status that is not terminal and is not CANCELLING.
        return [
            cls.PENDING,
            cls.STARTING,
            cls.RUNNING,
            cls.RECOVERING,
        ]


_SPOT_STATUS_TO_COLOR = {
    ManagedJobStatus.PENDING: colorama.Fore.BLUE,
    ManagedJobStatus.STARTING: colorama.Fore.BLUE,
    ManagedJobStatus.RUNNING: colorama.Fore.GREEN,
    ManagedJobStatus.RECOVERING: colorama.Fore.CYAN,
    ManagedJobStatus.SUCCEEDED: colorama.Fore.GREEN,
    ManagedJobStatus.FAILED: colorama.Fore.RED,
    ManagedJobStatus.FAILED_PRECHECKS: colorama.Fore.RED,
    ManagedJobStatus.FAILED_SETUP: colorama.Fore.RED,
    ManagedJobStatus.FAILED_NO_RESOURCE: colorama.Fore.RED,
    ManagedJobStatus.FAILED_CONTROLLER: colorama.Fore.RED,
    ManagedJobStatus.CANCELLING: colorama.Fore.YELLOW,
    ManagedJobStatus.CANCELLED: colorama.Fore.YELLOW,
    # TODO(cooperc): backwards compatibility, remove this in v0.12.0
    ManagedJobStatus.DEPRECATED_SUBMITTED: colorama.Fore.BLUE,
}


class ManagedJobScheduleState(enum.Enum):
    """Captures the state of the job from the scheduler's perspective.

    A job that predates the introduction of the scheduler will be INVALID.

    A newly created job will be INACTIVE.  The following transitions are valid:
    - INACTIVE -> WAITING: The job is "submitted" to the scheduler, and its job
      controller can be started.
    - WAITING -> LAUNCHING: The job controller is starting by the scheduler and
      may proceed to sky.launch.
    - LAUNCHING -> ALIVE: The launch attempt was completed. It may have
      succeeded or failed. The job controller is not allowed to sky.launch again
      without transitioning to ALIVE_WAITING and then LAUNCHING.
    - LAUNCHING -> ALIVE_BACKOFF: The launch failed to find resources, and is
      in backoff waiting for resources.
    - ALIVE -> ALIVE_WAITING: The job controller wants to sky.launch again,
      either for recovery or to launch a subsequent task.
    - ALIVE_BACKOFF -> ALIVE_WAITING: The backoff period has ended, and the job
      controller wants to try to launch again.
    - ALIVE_WAITING -> LAUNCHING: The scheduler has determined that the job
      controller may launch again.
    - LAUNCHING, ALIVE, or ALIVE_WAITING -> DONE: The job controller is exiting
      and the job is in some terminal status. In the future it may be possible
      to transition directly from WAITING or even INACTIVE to DONE if the job is
      cancelled.

    You can see a state diagram in sky/jobs/README.md.

    There is no well-defined mapping from the managed job status to schedule
    state or vice versa. (In fact, schedule state is defined on the job and
    status on the task.)
    - INACTIVE or WAITING should only be seen when a job is PENDING.
    - ALIVE_BACKOFF should only be seen when a job is STARTING.
    - ALIVE_WAITING should only be seen when a job is RECOVERING, has multiple
      tasks, or needs to retry launching.
    - LAUNCHING and ALIVE can be seen in many different statuses.
    - DONE should only be seen when a job is in a terminal status.
    Since state and status transitions are not atomic, it may be possible to
    briefly observe inconsistent states, like a job that just finished but
    hasn't yet transitioned to DONE.
    """
    # This job may have been created before scheduler was introduced in #4458.
    # This state is not used by scheduler but just for backward compatibility.
    # TODO(cooperc): remove this in v0.11.0
    INVALID = None
    # The job should be ignored by the scheduler.
    INACTIVE = 'INACTIVE'
    # The job is waiting to transition to LAUNCHING for the first time. The
    # scheduler should try to transition it, and when it does, it should start
    # the job controller.
    WAITING = 'WAITING'
    # The job is already alive, but wants to transition back to LAUNCHING,
    # e.g. for recovery, or launching later tasks in the DAG. The scheduler
    # should try to transition it to LAUNCHING.
    ALIVE_WAITING = 'ALIVE_WAITING'
    # The job is running sky.launch, or soon will, using a limited number of
    # allowed launch slots.
    LAUNCHING = 'LAUNCHING'
    # The job is alive, but is in backoff waiting for resources - a special case
    # of ALIVE.
    ALIVE_BACKOFF = 'ALIVE_BACKOFF'
    # The controller for the job is running, but it's not currently launching.
    ALIVE = 'ALIVE'
    # The job is in a terminal state. (Not necessarily SUCCEEDED.)
    DONE = 'DONE'


# === Status transition functions ===
@_init_db
def set_job_info(job_id: int, name: str, workspace: str, entrypoint: str):
    assert _DB_PATH is not None
    with db_utils.safe_cursor(_DB_PATH) as cursor:
        cursor.execute(
            """\
            INSERT INTO job_info
            (spot_job_id, name, schedule_state, workspace, entrypoint)
            VALUES (?, ?, ?, ?, ?)""",
            (job_id, name, ManagedJobScheduleState.INACTIVE.value, workspace,
             entrypoint))


@_init_db
def set_pending(job_id: int, task_id: int, task_name: str, resources_str: str):
    """Set the task to pending state."""
    assert _DB_PATH is not None
    with db_utils.safe_cursor(_DB_PATH) as cursor:
        cursor.execute(
            """\
            INSERT INTO spot
            (spot_job_id, task_id, task_name, resources, status)
            VALUES (?, ?, ?, ?, ?)""",
            (job_id, task_id, task_name, resources_str,
             ManagedJobStatus.PENDING.value))


@_init_db
def set_starting(job_id: int, task_id: int, run_timestamp: str,
                 submit_time: float, resources_str: str,
                 specs: Dict[str, Union[str,
                                        int]], callback_func: CallbackType):
    """Set the task to starting state.

    Args:
        job_id: The managed job ID.
        task_id: The task ID.
        run_timestamp: The run_timestamp of the run. This will be used to
        determine the log directory of the managed task.
        submit_time: The time when the managed task is submitted.
        resources_str: The resources string of the managed task.
        specs: The specs of the managed task.
        callback_func: The callback function.
    """
    assert _DB_PATH is not None
    # Use the timestamp in the `run_timestamp` ('sky-2022-10...'), to make
    # the log directory and submission time align with each other, so as to
    # make it easier to find them based on one of the values.
    # Also, using the earlier timestamp should be closer to the term
    # `submit_at`, which represents the time the managed task is submitted.
    logger.info('Launching the spot cluster...')
    with db_utils.safe_cursor(_DB_PATH) as cursor:
        cursor.execute(
            """\
            UPDATE spot SET
            resources=(?),
            submitted_at=(?),
            status=(?),
            run_timestamp=(?),
            specs=(?)
            WHERE spot_job_id=(?) AND
            task_id=(?) AND
            status=(?) AND
            end_at IS null""",
            (resources_str, submit_time, ManagedJobStatus.STARTING.value,
             run_timestamp, json.dumps(specs), job_id, task_id,
             ManagedJobStatus.PENDING.value))
        if cursor.rowcount != 1:
            raise exceptions.ManagedJobStatusError(
                'Failed to set the task to starting. '
                f'({cursor.rowcount} rows updated)')
    # SUBMITTED is no longer used, but we keep it for backward compatibility.
    # TODO(cooperc): remove this in v0.12.0
    callback_func('SUBMITTED')
    callback_func('STARTING')


@_init_db
def set_backoff_pending(job_id: int, task_id: int):
    """Set the task to PENDING state if it is in backoff.

    This should only be used to transition from STARTING or RECOVERING back to
    PENDING.
    """
    assert _DB_PATH is not None
    with db_utils.safe_cursor(_DB_PATH) as cursor:
        cursor.execute(
            """\
            UPDATE spot SET status=(?)
            WHERE spot_job_id=(?) AND
            task_id=(?) AND
            status IN (?, ?) AND
            end_at IS null""", (ManagedJobStatus.PENDING.value, job_id, task_id,
                                ManagedJobStatus.STARTING.value,
                                ManagedJobStatus.RECOVERING.value))
        logger.debug('back to PENDING')
        if cursor.rowcount != 1:
            raise exceptions.ManagedJobStatusError(
                'Failed to set the task back to pending. '
                f'({cursor.rowcount} rows updated)')
    # Do not call callback_func here, as we don't use the callback for PENDING.


@_init_db
def set_restarting(job_id: int, task_id: int, recovering: bool):
    """Set the task back to STARTING or RECOVERING from PENDING.

    This should not be used for the initial transition from PENDING to STARTING.
    In that case, use set_starting instead. This function should only be used
    after using set_backoff_pending to transition back to PENDING during
    launch retry backoff.
    """
    assert _DB_PATH is not None
    target_status = ManagedJobStatus.STARTING.value
    if recovering:
        target_status = ManagedJobStatus.RECOVERING.value
    with db_utils.safe_cursor(_DB_PATH) as cursor:
        cursor.execute(
            """\
            UPDATE spot SET status=(?)
            WHERE spot_job_id=(?) AND
            task_id=(?) AND
            status=(?) AND
            end_at IS null""",
            (target_status, job_id, task_id, ManagedJobStatus.PENDING.value))
        logger.debug(f'back to {target_status}')
        if cursor.rowcount != 1:
            raise exceptions.ManagedJobStatusError(
                f'Failed to set the task back to {target_status}. '
                f'({cursor.rowcount} rows updated)')
    # Do not call callback_func here, as it should only be invoked for the
    # initial (pre-`set_backoff_pending`) transition to STARTING or RECOVERING.


@_init_db
def set_started(job_id: int, task_id: int, start_time: float,
                callback_func: CallbackType):
    """Set the task to started state."""
    assert _DB_PATH is not None
    logger.info('Job started.')
    with db_utils.safe_cursor(_DB_PATH) as cursor:
        cursor.execute(
            """\
            UPDATE spot SET status=(?), start_at=(?), last_recovered_at=(?)
            WHERE spot_job_id=(?) AND
            task_id=(?) AND
            status IN (?, ?) AND
            end_at IS null""",
            (
                ManagedJobStatus.RUNNING.value,
                start_time,
                start_time,
                job_id,
                task_id,
                ManagedJobStatus.STARTING.value,
                # If the task is empty, we will jump straight from PENDING to
                # RUNNING
                ManagedJobStatus.PENDING.value,
            ),
        )
        if cursor.rowcount != 1:
            raise exceptions.ManagedJobStatusError(
                f'Failed to set the task to started. '
                f'({cursor.rowcount} rows updated)')
    callback_func('STARTED')


@_init_db
def set_recovering(job_id: int, task_id: int, force_transit_to_recovering: bool,
                   callback_func: CallbackType):
    """Set the task to recovering state, and update the job duration."""
    assert _DB_PATH is not None
    logger.info('=== Recovering... ===')
    expected_status: List[str] = [ManagedJobStatus.RUNNING.value]
    status_str = 'status=(?)'
    if force_transit_to_recovering:
        # For the HA job controller, it is possible that the jobs came from any
        # processing status to recovering. But it should not be any terminal
        # status as such jobs will not be recovered; and it should not be
        # CANCELLING as we will directly trigger a cleanup.
        expected_status = [
            s.value for s in ManagedJobStatus.processing_statuses()
        ]
        question_mark_str = ', '.join(['?'] * len(expected_status))
        status_str = f'status IN ({question_mark_str})'
    # NOTE: if we are resuming from a controller failure and the previous status
    # is STARTING, the initial value of `last_recovered_at` might not be set
    # yet (default value -1). In this case, we should not add current timestamp.
    # Otherwise, the job duration will be incorrect (~55 years from 1970).
    current_time = time.time()
    with db_utils.safe_cursor(_DB_PATH) as cursor:
        cursor.execute(
            f"""\
                UPDATE spot SET
                status=(?),
                job_duration=CASE
                    WHEN last_recovered_at >= 0
                    THEN job_duration+(?)-last_recovered_at
                    ELSE job_duration
                END,
                last_recovered_at=CASE
                    WHEN last_recovered_at < 0
                    THEN (?)
                    ELSE last_recovered_at
                END
                WHERE spot_job_id=(?) AND
                task_id=(?) AND
                {status_str} AND
                end_at IS null""",
            (ManagedJobStatus.RECOVERING.value, current_time, current_time,
             job_id, task_id, *expected_status))
        if cursor.rowcount != 1:
            raise exceptions.ManagedJobStatusError(
                f'Failed to set the task to recovering. '
                f'({cursor.rowcount} rows updated)')
    callback_func('RECOVERING')


@_init_db
def set_recovered(job_id: int, task_id: int, recovered_time: float,
                  callback_func: CallbackType):
    """Set the task to recovered."""
    assert _DB_PATH is not None
    with db_utils.safe_cursor(_DB_PATH) as cursor:
        cursor.execute(
            """\
            UPDATE spot SET
            status=(?), last_recovered_at=(?), recovery_count=recovery_count+1
            WHERE spot_job_id=(?) AND
            task_id=(?) AND
            status=(?) AND
            end_at IS null""",
            (ManagedJobStatus.RUNNING.value, recovered_time, job_id, task_id,
             ManagedJobStatus.RECOVERING.value))
        if cursor.rowcount != 1:
            raise exceptions.ManagedJobStatusError(
                f'Failed to set the task to recovered. '
                f'({cursor.rowcount} rows updated)')
    logger.info('==== Recovered. ====')
    callback_func('RECOVERED')


@_init_db
def set_succeeded(job_id: int, task_id: int, end_time: float,
                  callback_func: CallbackType):
    """Set the task to succeeded, if it is in a non-terminal state."""
    assert _DB_PATH is not None
    with db_utils.safe_cursor(_DB_PATH) as cursor:
        cursor.execute(
            """\
            UPDATE spot SET
            status=(?), end_at=(?)
            WHERE spot_job_id=(?) AND
            task_id=(?) AND
            status=(?) AND
            end_at IS null""",
            (ManagedJobStatus.SUCCEEDED.value, end_time, job_id, task_id,
             ManagedJobStatus.RUNNING.value))
        if cursor.rowcount != 1:
            raise exceptions.ManagedJobStatusError(
                f'Failed to set the task to succeeded. '
                f'({cursor.rowcount} rows updated)')
    callback_func('SUCCEEDED')
    logger.info('Job succeeded.')


@_init_db
def set_failed(
    job_id: int,
    task_id: Optional[int],
    failure_type: ManagedJobStatus,
    failure_reason: str,
    callback_func: Optional[CallbackType] = None,
    end_time: Optional[float] = None,
    override_terminal: bool = False,
):
    """Set an entire job or task to failed.

    By default, don't override tasks that are already terminal (that is, for
    which end_at is already set).

    Args:
        job_id: The job id.
        task_id: The task id. If None, all non-finished tasks of the job will
            be set to failed.
        failure_type: The failure type. One of ManagedJobStatus.FAILED_*.
        failure_reason: The failure reason.
        end_time: The end time. If None, the current time will be used.
        override_terminal: If True, override the current status even if end_at
            is already set.
    """
    assert _DB_PATH is not None
    assert failure_type.is_failed(), failure_type
    end_time = time.time() if end_time is None else end_time

    fields_to_set: Dict[str, Any] = {
        'status': failure_type.value,
        'failure_reason': failure_reason,
    }
    with db_utils.safe_cursor(_DB_PATH) as cursor:
        previous_status = cursor.execute(
            'SELECT status FROM spot WHERE spot_job_id=(?)',
            (job_id,)).fetchone()[0]
        previous_status = ManagedJobStatus(previous_status)
        if previous_status == ManagedJobStatus.RECOVERING:
            # If the job is recovering, we should set the last_recovered_at to
            # the end_time, so that the end_at - last_recovered_at will not be
            # affect the job duration calculation.
            fields_to_set['last_recovered_at'] = end_time
        set_str = ', '.join(f'{k}=(?)' for k in fields_to_set)
        task_query_str = '' if task_id is None else 'AND task_id=(?)'
        task_value = [] if task_id is None else [
            task_id,
        ]

        if override_terminal:
            # Use COALESCE for end_at to avoid overriding the existing end_at if
            # it's already set.
            cursor.execute(
                f"""\
                UPDATE spot SET
                end_at = COALESCE(end_at, ?),
                {set_str}
                WHERE spot_job_id=(?) {task_query_str}""",
                (end_time, *list(fields_to_set.values()), job_id, *task_value))
        else:
            # Only set if end_at is null, i.e. the previous status is not
            # terminal.
            cursor.execute(
                f"""\
                UPDATE spot SET
                end_at = (?),
                {set_str}
                WHERE spot_job_id=(?) {task_query_str} AND end_at IS null""",
                (end_time, *list(fields_to_set.values()), job_id, *task_value))

        updated = cursor.rowcount > 0
    if callback_func and updated:
        callback_func('FAILED')
    logger.info(failure_reason)


@_init_db
def set_cancelling(job_id: int, callback_func: CallbackType):
    """Set tasks in the job as cancelling, if they are in non-terminal states.

    task_id is not needed, because we expect the job should be cancelled
    as a whole, and we should not cancel a single task.
    """
    assert _DB_PATH is not None
    with db_utils.safe_cursor(_DB_PATH) as cursor:
        rows = cursor.execute(
            """\
            UPDATE spot SET
            status=(?)
            WHERE spot_job_id=(?) AND end_at IS null""",
            (ManagedJobStatus.CANCELLING.value, job_id))
        updated = rows.rowcount > 0
    if updated:
        logger.info('Cancelling the job...')
        callback_func('CANCELLING')
    else:
        logger.info('Cancellation skipped, job is already terminal')


@_init_db
def set_cancelled(job_id: int, callback_func: CallbackType):
    """Set tasks in the job as cancelled, if they are in CANCELLING state.

    The set_cancelling should be called before this function.
    """
    assert _DB_PATH is not None
    with db_utils.safe_cursor(_DB_PATH) as cursor:
        rows = cursor.execute(
            """\
            UPDATE spot SET
            status=(?), end_at=(?)
            WHERE spot_job_id=(?) AND status=(?)""",
            (ManagedJobStatus.CANCELLED.value, time.time(), job_id,
             ManagedJobStatus.CANCELLING.value))
        updated = rows.rowcount > 0
    if updated:
        logger.info('Job cancelled.')
        callback_func('CANCELLED')
    else:
        logger.info('Cancellation skipped, job is not CANCELLING')


@_init_db
def set_local_log_file(job_id: int, task_id: Optional[int],
                       local_log_file: str):
    """Set the local log file for a job."""
    assert _DB_PATH is not None
    filter_str = 'spot_job_id=(?)'
    filter_args = [local_log_file, job_id]

    if task_id is not None:
        filter_str += ' AND task_id=(?)'
        filter_args.append(task_id)
    with db_utils.safe_cursor(_DB_PATH) as cursor:
        cursor.execute(
            'UPDATE spot SET local_log_file=(?) '
            f'WHERE {filter_str}', filter_args)


# ======== utility functions ========
@_init_db
def get_nonterminal_job_ids_by_name(name: Optional[str],
                                    all_users: bool = False) -> List[int]:
    """Get non-terminal job ids by name."""
    assert _DB_PATH is not None
    statuses = ', '.join(['?'] * len(ManagedJobStatus.terminal_statuses()))
    field_values = [
        status.value for status in ManagedJobStatus.terminal_statuses()
    ]

    job_filter = ''
    if name is None and not all_users:
        job_filter += 'AND (job_info.user_hash=(?)) '
        field_values.append(common_utils.get_user_hash())
    if name is not None:
        # We match the job name from `job_info` for the jobs submitted after
        # #1982, and from `spot` for the jobs submitted before #1982, whose
        # job_info is not available.
        job_filter += ('AND (job_info.name=(?) OR '
                       '(job_info.name IS NULL AND spot.task_name=(?))) ')
        field_values.extend([name, name])

    # Left outer join is used here instead of join, because the job_info does
    # not contain the managed jobs submitted before #1982.
    with db_utils.safe_cursor(_DB_PATH) as cursor:
        rows = cursor.execute(
            f"""\
            SELECT DISTINCT spot.spot_job_id
            FROM spot
            LEFT OUTER JOIN job_info
            ON spot.spot_job_id=job_info.spot_job_id
            WHERE status NOT IN
            ({statuses})
            {job_filter}
            ORDER BY spot.spot_job_id DESC""", field_values).fetchall()
        job_ids = [row[0] for row in rows if row[0] is not None]
        return job_ids


@_init_db
def get_schedule_live_jobs(job_id: Optional[int]) -> List[Dict[str, Any]]:
    """Get jobs from the database that have a live schedule_state.

    This should return job(s) that are not INACTIVE, WAITING, or DONE.  So a
    returned job should correspond to a live job controller process, with one
    exception: the job may have just transitioned from WAITING to LAUNCHING, but
    the controller process has not yet started.
    """
    assert _DB_PATH is not None
    job_filter = '' if job_id is None else 'AND spot_job_id=(?)'
    job_value = (job_id,) if job_id is not None else ()

    # Join spot and job_info tables to get the job name for each task.
    # We use LEFT OUTER JOIN mainly for backward compatibility, as for an
    # existing controller before #1982, the job_info table may not exist,
    # and all the managed jobs created before will not present in the
    # job_info.
    with db_utils.safe_cursor(_DB_PATH) as cursor:
        rows = cursor.execute(
            f"""\
            SELECT spot_job_id, schedule_state, controller_pid
            FROM job_info
            WHERE schedule_state not in (?, ?, ?)
            {job_filter}
            ORDER BY spot_job_id DESC""",
            (ManagedJobScheduleState.INACTIVE.value,
             ManagedJobScheduleState.WAITING.value,
             ManagedJobScheduleState.DONE.value, *job_value)).fetchall()
        jobs = []
        for row in rows:
            job_dict = {
                'job_id': row[0],
                'schedule_state': ManagedJobScheduleState(row[1]),
                'controller_pid': row[2],
            }
            jobs.append(job_dict)
        return jobs


@_init_db
def get_jobs_to_check_status(job_id: Optional[int] = None) -> List[int]:
    """Get jobs that need controller process checking.

    Args:
        job_id: Optional job ID to check. If None, checks all jobs.

    Returns a list of job_ids, including the following:
    - Jobs that have a schedule_state that is not DONE
    - Jobs have schedule_state DONE but are in a non-terminal status
    - Legacy jobs (that is, no schedule state) that are in non-terminal status
    """
    assert _DB_PATH is not None
    job_filter = '' if job_id is None else 'AND spot.spot_job_id=(?)'
    job_value = () if job_id is None else (job_id,)

    status_filter_str = ', '.join(['?'] *
                                  len(ManagedJobStatus.terminal_statuses()))
    terminal_status_values = [
        status.value for status in ManagedJobStatus.terminal_statuses()
    ]

    # Get jobs that are either:
    # 1. Have schedule state that is not DONE, or
    # 2. Have schedule state DONE AND are in non-terminal status (unexpected
    #    inconsistent state), or
    # 3. Have no schedule state (legacy) AND are in non-terminal status
    with db_utils.safe_cursor(_DB_PATH) as cursor:
        rows = cursor.execute(
            f"""\
            SELECT DISTINCT spot.spot_job_id
            FROM spot
            LEFT OUTER JOIN job_info
            ON spot.spot_job_id=job_info.spot_job_id
            WHERE (
                -- non-legacy jobs that are not DONE
                (job_info.schedule_state IS NOT NULL AND
                 job_info.schedule_state IS NOT ?)
                OR
                -- legacy or that are in non-terminal status or
                -- DONE jobs that are in non-terminal status
                ((-- legacy jobs
                  job_info.schedule_state IS NULL OR
                  -- non-legacy DONE jobs
                  job_info.schedule_state IS ?
                 ) AND
                 -- non-terminal
                 status NOT IN ({status_filter_str}))
            )
            {job_filter}
            ORDER BY spot.spot_job_id DESC""", [
                ManagedJobScheduleState.DONE.value,
                ManagedJobScheduleState.DONE.value, *terminal_status_values,
                *job_value
            ]).fetchall()
        return [row[0] for row in rows if row[0] is not None]


@_init_db
def get_all_job_ids_by_name(name: Optional[str]) -> List[int]:
    """Get all job ids by name."""
    assert _DB_PATH is not None
    name_filter = ''
    field_values = []
    if name is not None:
        # We match the job name from `job_info` for the jobs submitted after
        # #1982, and from `spot` for the jobs submitted before #1982, whose
        # job_info is not available.
        name_filter = ('WHERE (job_info.name=(?) OR '
                       '(job_info.name IS NULL AND spot.task_name=(?)))')
        field_values = [name, name]

    # Left outer join is used here instead of join, because the job_info does
    # not contain the managed jobs submitted before #1982.
    with db_utils.safe_cursor(_DB_PATH) as cursor:
        rows = cursor.execute(
            f"""\
            SELECT DISTINCT spot.spot_job_id
            FROM spot
            LEFT OUTER JOIN job_info
            ON spot.spot_job_id=job_info.spot_job_id
            {name_filter}
            ORDER BY spot.spot_job_id DESC""", field_values).fetchall()
        job_ids = [row[0] for row in rows if row[0] is not None]
        return job_ids


@_init_db
def _get_all_task_ids_statuses(
        job_id: int) -> List[Tuple[int, ManagedJobStatus]]:
    assert _DB_PATH is not None
    with db_utils.safe_cursor(_DB_PATH) as cursor:
        id_statuses = cursor.execute(
            """\
            SELECT task_id, status FROM spot
            WHERE spot_job_id=(?)
            ORDER BY task_id ASC""", (job_id,)).fetchall()
        return [(row[0], ManagedJobStatus(row[1])) for row in id_statuses]


@_init_db
def get_job_status_with_task_id(job_id: int,
                                task_id: int) -> Optional[ManagedJobStatus]:
    assert _DB_PATH is not None
    with db_utils.safe_cursor(_DB_PATH) as cursor:
        status = cursor.execute(
            """\
            SELECT status FROM spot
            WHERE spot_job_id=(?) AND task_id=(?)""",
            (job_id, task_id)).fetchone()
        return ManagedJobStatus(status[0]) if status else None


def get_num_tasks(job_id: int) -> int:
    return len(_get_all_task_ids_statuses(job_id))


def get_latest_task_id_status(
        job_id: int) -> Union[Tuple[int, ManagedJobStatus], Tuple[None, None]]:
    """Returns the (task id, status) of the latest task of a job.

    The latest means the task that is currently being executed or to be started
    by the controller process. For example, in a managed job with 3 tasks, the
    first task is succeeded, and the second task is being executed. This will
    return (1, ManagedJobStatus.RUNNING).

    If the job_id does not exist, (None, None) will be returned.
    """
    id_statuses = _get_all_task_ids_statuses(job_id)
    if not id_statuses:
        return None, None
    task_id, status = next(
        ((tid, st) for tid, st in id_statuses if not st.is_terminal()),
        id_statuses[-1],
    )
    # Unpack the tuple first, or it triggers a Pylint's bug on recognizing
    # the return type.
    return task_id, status


def get_status(job_id: int) -> Optional[ManagedJobStatus]:
    _, status = get_latest_task_id_status(job_id)
    return status


@_init_db
def get_failure_reason(job_id: int) -> Optional[str]:
    """Get the failure reason of a job.

    If the job has multiple tasks, we return the first failure reason.
    """
    assert _DB_PATH is not None
    with db_utils.safe_cursor(_DB_PATH) as cursor:
        reason = cursor.execute(
            """\
            SELECT failure_reason FROM spot
            WHERE spot_job_id=(?)
            ORDER BY task_id ASC""", (job_id,)).fetchall()
        reason = [r[0] for r in reason if r[0] is not None]
        if not reason:
            return None
        return reason[0]


@_init_db
def get_managed_jobs(job_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """Get managed jobs from the database."""
    assert _DB_PATH is not None
    job_filter = '' if job_id is None else f'WHERE spot.spot_job_id={job_id}'

    # Join spot and job_info tables to get the job name for each task.
    # We use LEFT OUTER JOIN mainly for backward compatibility, as for an
    # existing controller before #1982, the job_info table may not exist,
    # and all the managed jobs created before will not present in the
    # job_info.
    # Note: we will get the user_hash here, but don't try to call
    # global_user_state.get_user() on it. This runs on the controller, which may
    # not have the user info. Prefer to do it on the API server side.
    with db_utils.safe_cursor(_DB_PATH) as cursor:
        rows = cursor.execute(f"""\
            SELECT *
            FROM spot
            LEFT OUTER JOIN job_info
            ON spot.spot_job_id=job_info.spot_job_id
            {job_filter}
            ORDER BY spot.spot_job_id DESC, spot.task_id ASC""").fetchall()
        jobs = []
        for row in rows:
            job_dict = dict(zip(columns, row))
            job_dict['status'] = ManagedJobStatus(job_dict['status'])
            job_dict['schedule_state'] = ManagedJobScheduleState(
                job_dict['schedule_state'])
            if job_dict['job_name'] is None:
                job_dict['job_name'] = job_dict['task_name']

            # Add user YAML content for managed jobs.
            yaml_path = job_dict.get('original_user_yaml_path')
            if yaml_path:
                try:
                    with open(yaml_path, 'r', encoding='utf-8') as f:
                        job_dict['user_yaml'] = f.read()
                except (FileNotFoundError, IOError, OSError):
                    job_dict['user_yaml'] = None
            else:
                job_dict['user_yaml'] = None

            jobs.append(job_dict)
        return jobs


@_init_db
def get_task_name(job_id: int, task_id: int) -> str:
    """Get the task name of a job."""
    assert _DB_PATH is not None
    with db_utils.safe_cursor(_DB_PATH) as cursor:
        task_name = cursor.execute(
            """\
            SELECT task_name FROM spot
            WHERE spot_job_id=(?)
            AND task_id=(?)""", (job_id, task_id)).fetchone()
        return task_name[0]


@_init_db
def get_latest_job_id() -> Optional[int]:
    """Get the latest job id."""
    assert _DB_PATH is not None
    with db_utils.safe_cursor(_DB_PATH) as cursor:
        rows = cursor.execute("""\
            SELECT spot_job_id FROM spot
            WHERE task_id=0
            ORDER BY submitted_at DESC LIMIT 1""")
        for (job_id,) in rows:
            return job_id
        return None


@_init_db
def get_task_specs(job_id: int, task_id: int) -> Dict[str, Any]:
    assert _DB_PATH is not None
    with db_utils.safe_cursor(_DB_PATH) as cursor:
        task_specs = cursor.execute(
            """\
            SELECT specs FROM spot
            WHERE spot_job_id=(?) AND task_id=(?)""",
            (job_id, task_id)).fetchone()
        return json.loads(task_specs[0])


@_init_db
def get_local_log_file(job_id: int, task_id: Optional[int]) -> Optional[str]:
    """Get the local log directory for a job."""
    assert _DB_PATH is not None
    filter_str = 'spot_job_id=(?)'
    filter_args = [job_id]
    if task_id is not None:
        filter_str += ' AND task_id=(?)'
        filter_args.append(task_id)
    with db_utils.safe_cursor(_DB_PATH) as cursor:
        local_log_file = cursor.execute(
            f'SELECT local_log_file FROM spot '
            f'WHERE {filter_str}', filter_args).fetchone()
        return local_log_file[-1] if local_log_file else None


# === Scheduler state functions ===
# Only the scheduler should call these functions. They may require holding the
# scheduler lock to work correctly.


@_init_db
def scheduler_set_waiting(job_id: int, dag_yaml_path: str,
                          original_user_yaml_path: str, env_file_path: str,
                          user_hash: str, priority: int) -> bool:
    """Do not call without holding the scheduler lock.

    Returns: Whether this is a recovery run or not.
        If this is a recovery run, the job may already be in the WAITING
        state and the update will not change the schedule_state (hence the
        updated_count will be 0). In this case, we return True.
        Otherwise, we return False.
    """
    assert _DB_PATH is not None
    with db_utils.safe_cursor(_DB_PATH) as cursor:
        updated_count = cursor.execute(
            'UPDATE job_info SET '
            'schedule_state = (?), dag_yaml_path = (?), '
            'original_user_yaml_path = (?), env_file_path = (?), '
            '  user_hash = (?), priority = (?) '
            'WHERE spot_job_id = (?) AND schedule_state = (?)',
            (ManagedJobScheduleState.WAITING.value, dag_yaml_path,
             original_user_yaml_path, env_file_path, user_hash, priority,
             job_id, ManagedJobScheduleState.INACTIVE.value)).rowcount
        # For a recovery run, the job may already be in the WAITING state.
        assert updated_count <= 1, (job_id, updated_count)
        return updated_count == 0


@_init_db
def scheduler_set_launching(job_id: int,
                            current_state: ManagedJobScheduleState) -> None:
    """Do not call without holding the scheduler lock."""
    assert _DB_PATH is not None
    with db_utils.safe_cursor(_DB_PATH) as cursor:
        updated_count = cursor.execute(
            'UPDATE job_info SET '
            'schedule_state = (?) '
            'WHERE spot_job_id = (?) AND schedule_state = (?)',
            (ManagedJobScheduleState.LAUNCHING.value, job_id,
             current_state.value)).rowcount
        assert updated_count == 1, (job_id, updated_count)


@_init_db
def scheduler_set_alive(job_id: int) -> None:
    """Do not call without holding the scheduler lock."""
    assert _DB_PATH is not None
    with db_utils.safe_cursor(_DB_PATH) as cursor:
        updated_count = cursor.execute(
            'UPDATE job_info SET '
            'schedule_state = (?) '
            'WHERE spot_job_id = (?) AND schedule_state = (?)',
            (ManagedJobScheduleState.ALIVE.value, job_id,
             ManagedJobScheduleState.LAUNCHING.value)).rowcount
        assert updated_count == 1, (job_id, updated_count)


@_init_db
def scheduler_set_alive_backoff(job_id: int) -> None:
    """Do not call without holding the scheduler lock."""
    assert _DB_PATH is not None
    with db_utils.safe_cursor(_DB_PATH) as cursor:
        updated_count = cursor.execute(
            'UPDATE job_info SET '
            'schedule_state = (?) '
            'WHERE spot_job_id = (?) AND schedule_state = (?)',
            (ManagedJobScheduleState.ALIVE_BACKOFF.value, job_id,
             ManagedJobScheduleState.LAUNCHING.value)).rowcount
        assert updated_count == 1, (job_id, updated_count)


@_init_db
def scheduler_set_alive_waiting(job_id: int) -> None:
    """Do not call without holding the scheduler lock."""
    assert _DB_PATH is not None
    with db_utils.safe_cursor(_DB_PATH) as cursor:
        updated_count = cursor.execute(
            'UPDATE job_info SET '
            'schedule_state = (?) '
            'WHERE spot_job_id = (?) AND schedule_state IN (?, ?)',
            (ManagedJobScheduleState.ALIVE_WAITING.value, job_id,
             ManagedJobScheduleState.ALIVE.value,
             ManagedJobScheduleState.ALIVE_BACKOFF.value)).rowcount
        assert updated_count == 1, (job_id, updated_count)


@_init_db
def scheduler_set_done(job_id: int, idempotent: bool = False) -> None:
    """Do not call without holding the scheduler lock."""
    assert _DB_PATH is not None
    with db_utils.safe_cursor(_DB_PATH) as cursor:
        updated_count = cursor.execute(
            'UPDATE job_info SET '
            'schedule_state = (?) '
            'WHERE spot_job_id = (?) AND schedule_state != (?)',
            (ManagedJobScheduleState.DONE.value, job_id,
             ManagedJobScheduleState.DONE.value)).rowcount
        if not idempotent:
            assert updated_count == 1, (job_id, updated_count)


@_init_db
def set_job_controller_pid(job_id: int, pid: int):
    assert _DB_PATH is not None
    with db_utils.safe_cursor(_DB_PATH) as cursor:
        updated_count = cursor.execute(
            'UPDATE job_info SET '
            'controller_pid = (?) '
            'WHERE spot_job_id = (?)', (pid, job_id)).rowcount
        assert updated_count == 1, (job_id, updated_count)


@_init_db
def get_job_schedule_state(job_id: int) -> ManagedJobScheduleState:
    assert _DB_PATH is not None
    with db_utils.safe_cursor(_DB_PATH) as cursor:
        state = cursor.execute(
            'SELECT schedule_state FROM job_info WHERE spot_job_id = (?)',
            (job_id,)).fetchone()[0]
        return ManagedJobScheduleState(state)


@_init_db
def get_num_launching_jobs() -> int:
    assert _DB_PATH is not None
    with db_utils.safe_cursor(_DB_PATH) as cursor:
        return cursor.execute(
            'SELECT COUNT(*) '
            'FROM job_info '
            'WHERE schedule_state = (?)',
            (ManagedJobScheduleState.LAUNCHING.value,)).fetchone()[0]


@_init_db
def get_num_alive_jobs() -> int:
    assert _DB_PATH is not None
    with db_utils.safe_cursor(_DB_PATH) as cursor:
        return cursor.execute(
            'SELECT COUNT(*) '
            'FROM job_info '
            'WHERE schedule_state IN (?, ?, ?, ?)',
            (ManagedJobScheduleState.ALIVE_WAITING.value,
             ManagedJobScheduleState.LAUNCHING.value,
             ManagedJobScheduleState.ALIVE.value,
             ManagedJobScheduleState.ALIVE_BACKOFF.value)).fetchone()[0]


@_init_db
def get_waiting_job() -> Optional[Dict[str, Any]]:
    """Get the next job that should transition to LAUNCHING.

    Selects the highest-priority WAITING or ALIVE_WAITING job, provided its
    priority is greater than or equal to any currently LAUNCHING or
    ALIVE_BACKOFF job.

    Backwards compatibility note: jobs submitted before #4485 will have no
    schedule_state and will be ignored by this SQL query.
    """
    assert _DB_PATH is not None
    with db_utils.safe_cursor(_DB_PATH) as cursor:
        # Get the highest-priority WAITING or ALIVE_WAITING job whose priority
        # is greater than or equal to the highest priority LAUNCHING or
        # ALIVE_BACKOFF job's priority.
        waiting_job_row = cursor.execute(
            'SELECT spot_job_id, schedule_state, dag_yaml_path, env_file_path '
            'FROM job_info '
            'WHERE schedule_state IN (?, ?) '
            'AND priority >= COALESCE('
            '    (SELECT MAX(priority) '
            '     FROM job_info '
            '     WHERE schedule_state IN (?, ?)), '
            '    0'
            ')'
            'ORDER BY priority DESC, spot_job_id ASC LIMIT 1',
            (ManagedJobScheduleState.WAITING.value,
             ManagedJobScheduleState.ALIVE_WAITING.value,
             ManagedJobScheduleState.LAUNCHING.value,
             ManagedJobScheduleState.ALIVE_BACKOFF.value)).fetchone()

        if waiting_job_row is None:
            return None

        return {
            'job_id': waiting_job_row[0],
            'schedule_state': ManagedJobScheduleState(waiting_job_row[1]),
            'dag_yaml_path': waiting_job_row[2],
            'env_file_path': waiting_job_row[3],
        }


@_init_db
def get_workspace(job_id: int) -> str:
    """Get the workspace of a job."""
    assert _DB_PATH is not None
    with db_utils.safe_cursor(_DB_PATH) as cursor:
        workspace = cursor.execute(
            'SELECT workspace FROM job_info WHERE spot_job_id = (?)',
            (job_id,)).fetchone()
        job_workspace = workspace[0] if workspace else None
        if job_workspace is None:
            return constants.SKYPILOT_DEFAULT_WORKSPACE
        return job_workspace
