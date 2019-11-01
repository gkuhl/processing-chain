#!/usr/bin/env python
# -*- coding: utf-8 -*-

from datetime import datetime, timedelta

import importlib
import logging
import os
import sys
import time
import shutil
import argparse

import jobs
from jobs import tools


default_jobs = {
    tools.Target.COSMO: ["meteo", "icbc", "emissions", "biofluxes", "int2lm",
                         "post_int2lm", "cosmo", "post_cosmo"],
    tools.Target.COSMOART: ["meteo", "icbc", "emissions", "obs_nudging",
                            "photo_rate", "int2lm", "cosmo", "post_cosmo"]
}


def parse_arguments():
    """Parse the command line arguments given to this script

    Returns
    -------
    Namespace-object
    """
    parser = argparse.ArgumentParser(description="Run the processing chain.")

    parser.add_argument("casenames",
                        nargs='+',
                        help="List of identifiers for the runs. "
                        "The config-files for each run is assumed "
                        "to be in cases/<casename>/. The runs are executed "
                        "sequentially in the order they're given here.")

    times_help = ("Triplet of {date hstart hstop} | "
                  "date: Startdate of the run in the format "
                  "yyyy-mm-dd | "
                  "hstart: Time on the startdate when the "
                  "simulation starts. If this is zero, the "
                  "simulation starts at midnight of the +startdate. | "
                  "hstop: Length of the simulation in hours. The "
                  "simulation runs until startdate + hstart + "
                  "hstop. Depending on your config.py settings, "
                  "processing-chain will split up the simulation "
                  "and perform several restarts before reaching the "
                  "stopping-time.")
    parser.add_argument("times",
                        nargs=3,
                        help=times_help)

    jobs_help = ("List of job-names to be executed. A job is a .py-"
                 "file in jobs/ with a main()-function which "
                 "handles one aspect of the processing chain, for "
                 "example copying meteo-input data or launching a "
                 "job for int2lm. "
                 "Jobs are executed in the order in which they are "
                 "given here. "
                 "If no jobs are given, the default that will be "
                 "executed is: COSMO: {} | COSMOART : {}"
                 .format(default_jobs[tools.Target.COSMO],
                         default_jobs[tools.Target.COSMOART]))
    parser.add_argument("-j", "--jobs",
                        nargs='*',
                        dest="job_list",
                        help=jobs_help,
                        default=None)

    force_help = ("Force the processing chain to redo all specified jobs,"
                  " even if they have been started already or were finished"
                  " previously. WARNING: Only logfiles get deleted,"
                  " other effects of a given job (copied files etc.)"
                  " are simply overwritten. This may cause errors.")
    parser.add_argument("-f", "--force",
                        action='store_true',
                        help=force_help)

    tries_help = ("Amount of tries before crashing."
                  " Default is 1.")
    parser.add_argument("-t", "--try",
                        help=tries_help,
                        dest="ntry",
                        type=int,
                        default=1)


    args = parser.parse_args()
    args.startdate = args.times[0]
    args.hstart = int(args.times[1])
    args.hstop = int(args.times[2])

    return args


def load_config_file(casename, cfg):
    """Load the config file.

    Looks for the config file in ``cases/casename/config.py`` and then imports
    it as a module. This lets the config file contain python statements which
    are evaluated on import.

    If this is not the first config-file to be imported by run_chain.py, the
    module has to be reloaded to overwrite the values of the old case.

    Access variables declared in the config-file (``myval = 9``) with
    ``cfg.myval``.

    Add new variables with::

        setattr(cfg, 'myval', 9)

    Parameters
    ----------
    casename : str
        Name of the folder in cases/ where the configuration files are stored
    cfg : module or None
        If cfg is None, the module is freshly imported. If it is a module
        object, that module is reloaded.

    Returns
    -------
    config-object
        Object with all variables as attributes
    """
    cfg_path = os.path.join('cases', casename, 'config')

    if not os.path.exists(os.path.dirname(cfg_path)):
        all_cases = [path.name for path in os.scandir('cases') if path.is_dir]
        closest_name = min([(tools.levenshtein(casename, name), name)
                            for name in all_cases], key=lambda x: x[0])[1]
        raise FileNotFoundError("Case-directory '{}' not found, did you "
                                "mean '{}'?".format(casename, closest_name))

    sys.path.append(os.path.dirname(cfg_path))

    try:
        if cfg is None:
            cfg = importlib.import_module(os.path.basename(cfg_path))
        else:
            cfg = importlib.reload(cfg)
    except ModuleNotFoundError:
        raise FileNotFoundError("No file 'config.py' in " +
                                os.path.dirname(cfg_path))

    # so that a different cfg-file can be imported later
    sys.path.pop()

    return cfg


def set_simulation_type(cfg):
    """Detect if the chain targets cosmo or cosmoart and if there is a subtarget.

    Check if a target was provided in the config-object. If no target is
    provided, set the target to cosmo in the config-object.

    Check if a subtarget was provided in the config-object. Subtargets
    provide a way to customize the behaviour of the processing chain
    for different types of simulations.

    Raise a RuntimeError if a unsupported target or subtarget is given in cfg.
    You can add targets and subtargets in the jobs/tools/__init__.py file.

    Translates the target and subtarget from string to enum.

    Parameters
    ----------
    cfg : config-object
    """
    default = 'cosmo'
    target_str = getattr(cfg, 'target', default)
    try:
        target_enum = tools.str_to_enum[target_str.lower()]
    except KeyError:
        raise ValueError("The target of the chain must be one of {}"
                         .format(list(tools.str_to_enum.keys())))
    setattr(cfg, 'target', target_enum)

    subtarget_str = getattr(cfg, 'subtarget', 'none')
    try:
        subtarget_enum = tools.str_to_enum[subtarget_str.lower()]
    except KeyError:
        raise ValueError("The target of the chain must be one of {}"
                         .format(list(tools.str_to_enum.keys())))
    setattr(cfg.target, 'subtarget', subtarget_enum)


def run_chain(work_root, cfg, start_time, hstart, hstop, job_names, force):
    """Run chain ignoring already finished jobs.

    Sets configuration values derived from user-provided ones, for example the
    folder-structure inside the working directory.

    Sets up the logging module used by the jobs.

    Creates directories for each job.

    Decides which jobs to run and then runs them; first it checks wether the
    job was already executed or is currently running (depending on the logging
    file of the job). Then if the job has to be run, it calls the main()-
    function of the job. If force is True, the logging-file of the job will
    be deleted (if it exists) and the job will be executed regardless.

    Parameters
    ----------
    work_root : str
        The path to the directory in which the chain writes files during
        execution (typically scratch)
    cfg : config-object
        Object holding all user-configuration parameters as attributes
    start_time : datetime-object
        The startdate of the simulation
    hstart : int
        Offset (in hours) of the actual start from the startdate (start param)
    hstop : int
        Length of simulation (in hours)
    job_names : list of str
        List of the names of jobs to execute on every timeslice.
        Jobs are ``.py`` files in the ``jobs/`` directory with a ``main()``
        function that will be called from ``run_chain()``.
    force : bool
        If True will do job regardless of completion status
    """
    # ini date and forecast time (ignore meteo times)
    inidate = int((start_time - datetime(1970,1,1)).total_seconds())
    inidate_yyyymmddhh = start_time.strftime('%Y%m%d%H')
    forecasttime = '%d' % (hstop - hstart)
    setattr(cfg, 'inidate', inidate)
    setattr(cfg, 'inidate_yyyymmddhh',inidate_yyyymmddhh)
    setattr(cfg, 'hstart', hstart)
    setattr(cfg, 'hstop', hstop)
    forecasttime = '%d' % (hstop - hstart)
    inidate_int2lm_yyyymmddhh = (start_time + timedelta(hours=hstart)
                                ).strftime('%Y%m%d%H')

    if cfg.target.subtarget is tools.Subtarget.SPINUP:
        if cfg.first_one: # first run in spinup
            chain_root_last_run = ''
        else: # consecutive runs in spinup 
            inidate_yyyymmddhh_spinup = (start_time - timedelta(hours=cfg.spinup)
                                        ).strftime('%Y%m%d%H')
            setattr(cfg, 'inidate_yyyymmddhh', inidate_yyyymmddhh_spinup)
            setattr(cfg, 'hstart', 0)
            setattr(cfg, 'hstop', hstop + cfg.spinup)
            forecasttime = '%d' % (hstop + cfg.spinup)
            inidate_yyyymmddhh_last_run = (start_time - timedelta(hours=cfg.restart_step)
                                          ).strftime('%Y%m%d%H')
            if cfg.second_one: # second run (i.e., get job_id from first run)
                job_id_last_run = '%s_%d_%d' % (inidate_yyyymmddhh_last_run,
                                                                    0, hstop)
            else: # all other runs
                job_id_last_run = '%s_%d_%d' % (inidate_yyyymmddhh_last_run,
                                                0 - cfg.spinup, hstop)
            chain_root_last_run = os.path.join(work_root, cfg.casename,
                                               job_id_last_run)

    setattr(cfg, 'forecasttime', forecasttime)

    # int2lm processing always starts at hstart=0 and we modify inidate instead
    setattr(cfg, 'inidate_int2lm_yyyymmddhh', inidate_int2lm_yyyymmddhh)
    setattr(cfg, 'hstart_int2lm', '0')
    setattr(cfg, 'hstop_int2lm', forecasttime)

    # chain
    job_id = '%s_%d_%d' % (inidate_yyyymmddhh, hstart, hstop)
    chain_root = os.path.join(work_root, cfg.casename, job_id)
    setattr(cfg, 'chain_root', chain_root)

    # INT2LM
    setattr(cfg, 'int2lm_base', os.path.join(chain_root, 'int2lm'))
    setattr(cfg, 'int2lm_input', os.path.join(chain_root, 'int2lm', 'input'))
    setattr(cfg, 'int2lm_work', os.path.join(chain_root, 'int2lm', 'run'))
    setattr(cfg, 'int2lm_output', os.path.join(chain_root, 'int2lm', 'output'))

    # COSMO
    setattr(cfg, 'cosmo_base', os.path.join(chain_root, 'cosmo'))
    setattr(cfg, 'cosmo_input', os.path.join(chain_root, 'cosmo', 'input'))
    setattr(cfg, 'cosmo_work', os.path.join(chain_root, 'cosmo', 'run'))
    setattr(cfg, 'cosmo_output', os.path.join(chain_root, 'cosmo', 'output'))

    # constraint (gpu or mc)
    if hasattr(cfg, 'constraint'):
        assert cfg.constraint in ['gpu', 'mc'], ("Unknown constraint, use"
                                                 "gpu or mc")
    else:
        # set default constraint
        if cfg.target is tools.Target.COSMOART:
            setattr(cfg, 'constraint', 'mc')
        else:
            setattr(cfg, 'constraint', 'gpu')

    # asynchronous I/O
    if cfg.cosmo_np_io == 0:
        setattr(cfg, 'lasync_io', '.FALSE.')
        setattr(cfg, 'num_iope_percomm', 0)
    else:
        setattr(cfg, 'lasync_io', '.TRUE.')
        setattr(cfg, 'num_iope_percomm', 1)

    if cfg.target.subtarget is tools.Subtarget.SPINUP:
        setattr(cfg, 'last_cosmo_output',
                os.path.join(chain_root_last_run, 'cosmo', 'output'))
        # No restart for spinup simulations (= default values for no restart)
        setattr(cfg, 'cosmo_restart_out', '')
        setattr(cfg, 'cosmo_restart_in', '')
    elif cfg.target is not tools.Target.COSMOART:
        job_id_last_run = '%s_%d_%d' % (inidate_yyyymmddhh,
                                        hstart - cfg.restart_step, hstart)
        chain_root_last_run = os.path.join(work_root, cfg.casename,
                                          job_id_last_run)
        # Set restart directories
        setattr(cfg, 'cosmo_restart_out', os.path.join(chain_root,
                                                       'cosmo', 'restart'))
        setattr(cfg, 'cosmo_restart_in', os.path.join(chain_root_last_run,
                                                      'cosmo', 'restart'))

    if cfg.target is tools.Target.COSMOART:
        # no restarts in cosmoart
        setattr(cfg, 'restart_step', hstop - hstart)

    # if nested run: use output of mother-simulation
    if cfg.target is tools.Target.COSMOART and not os.path.isdir(cfg.meteo_dir):
        # if ifs_hres_dir doesn't point to a directory,
        # it is the name of the mother run
        mother_name = cfg.meteo_dir
        cfg.meteo_dir = os.path.join(work_root,
                                     mother_name,
                                     job_id,
                                     'cosmo',
                                     'output')

        cfg.meteo_inc = 1
        cfg.meteo_prefix = 'lffd'

    # logging
    log_working_dir = os.path.join(chain_root, 'checkpoints', 'working')
    log_finished_dir = os.path.join(chain_root, 'checkpoints', 'finished')
    setattr(cfg, 'log_working_dir', log_working_dir)
    setattr(cfg, 'log_finished_dir', log_finished_dir)

    # create working dirs
    tools.create_dir(chain_root, "chain_root")
    tools.create_dir(log_working_dir, "log_working")
    tools.create_dir(log_finished_dir, "log_finished")

    # number of levels and switch for unit conversion for 'reduce_output' job
    if not hasattr(cfg, 'output_levels'):
        setattr(cfg, 'output_levels', -1)
    if not hasattr(cfg, 'convert_gas'):
        setattr(cfg, 'convert_gas',True)

    # run jobs (if required)
    for job in job_names:

        # mapping of scripts in jobs with their arguments

        # if job == 'meteo':
        #     job.meteo.main(start_time, hstart, hstop, cfg)
        #     continue

        skip = False

        # if exists job is currently worked on or has been finished
        if os.path.exists(os.path.join(log_working_dir, job)):
            if not force:
                while True:
                    if os.path.exists( os.path.join(log_finished_dir, job) ):
                        print('Skip "%s" for chain "%s"' % (job, job_id))
                        skip = True
                        break
                    else:
                        print('Wait for "%s" of chain "%s"' % (job, job_id))
                        sys.stdout.flush()
                        for _ in range(3000):
                            time.sleep(0.1)
            else:
                os.remove(os.path.join(log_working_dir, job))
                try:
                    os.remove(os.path.join(log_finished_dir, job))
                except FileNotFoundError:
                    pass

        if not skip:
            print('Process "%s" for chain "%s"' % (job, job_id))
            sys.stdout.flush()
            
            try_count = args.ntry
            while try_count>0:
                try_count-=1
                try:
                    # Change the log file
                    logfile=os.path.join(cfg.log_working_dir,job)
                    logfile_finish=os.path.join(cfg.log_finished_dir,job)
                    tools.change_logfile(logfile)

                    # Launch the job
                    to_call = getattr(jobs,job)
                    to_call.main(start_time,hstart,hstop,cfg)

                    shutil.copy(logfile, logfile_finish)

                    exitcode=0
                    try_count=0
                except:
                    subject = "ERROR or TIMEOUT in job '%s' for chain '%s'" % (job,
                              job_id)
                    logging.exception(subject)
                    with open(os.path.join(log_working_dir, job)) as logfile:
                        message = logfile.read()
                    tools.send_mail(cfg.mail_address, subject, message)
                    if try_count ==0:
                        raise RuntimeError(subject)

                    
                
            # except AttributeError:
            #     print(job+".py not found so running the bash script instead")
            #     exitcode = call_bash_function(
            #         os.path.join(cfg.chain_src_dir, 'jobs', '%s.bash' % job),
            #         job
            #     )
                
            if exitcode != 0 or not os.path.exists(os.path.join(log_finished_dir, job)):
                subject = "ERROR or TIMEOUT in job '%s' for chain '%s'" % (job,
                          job_id)
                with open(os.path.join(log_working_dir, job)) as logfile:
                    message = logfile.read()
                tools.send_mail(cfg.mail_address, subject, message)
                raise RuntimeError(subject)


def restart_runs(work_root, cfg, start, hstart, hstop, job_names, force):
    """Starts the subchains in the specified intervals.
    
    Slices the total runtime of the chain according to ``cfg.restart_step``.
    Calls ``run_chain()`` for each step.
    
    Parameters
    ----------
    work_root : str
        The path to the directory in which the chain writes files during
        execution (typically scratch)
    cfg : config-object
        Object holding all user-configuration parameters as attributes
    start : datetime-object
        The startdate
    hstart : int
        Offset (in hours) of the actual start from the startdate (start param)
    hstop : int
        Length of simulation (in hours)
    job_names : list of str
        List of the names of jobs to execute on every timeslice.
        Jobs are .py files in the jobs/ directory with a main() function
        that will be called from run_chain().
    force : bool
        If True will do job regardless of completion status
    """
    # run restarts
    for time in tools.iter_hours(start, hstart, hstop, cfg.restart_step):
        sub_hstart = (time - start).total_seconds() / 3600.0
        runtime = min(cfg.restart_step, hstop - sub_hstart)
        if runtime == 0:
            # don't start simuation with 0 runtime
            continue
        sub_hstop = sub_hstart + runtime

        print("Starting run with starttime {}".format(time))

        run_chain(work_root = work_root,
                  cfg = cfg,
                  start_time = start,
                  hstart = sub_hstart,
                  hstop = sub_hstop,
                  job_names = job_names,
                  force = force)


def restart_runs_spinup(work_root, cfg, start, hstart, hstop, job_names, force):
    """Starts the subchains in the specified intervals.
    
    Slices the total runtime of the chain according to ``cfg.restart_step``.
    Calls ``run_chain()`` for each step.

    Runs custom "restarts" (= simulations with spin-up and tracer recycling).
    The first simulation is a normal one, with ``run_time = cfg.restart_step``.
    Consecutive simulations start at
    ``start + N * cfg.restart_step - cfg.spinup``.
    
    Parameters
    ----------
    work_root : str
        The path to the directory in which the chain writes files during
        execution (typically scratch)
    cfg : config-object
        Object holding all user-configuration parameters as attributes
    start : datetime-object
        The startdate
    hstart : int
        Offset (in hours) of the actual start from the startdate (start param)
    hstop : int
        Length of simulation (in hours)
    job_names : list of str
        List of the names of jobs to execute on every timeslice.
        Jobs are .py files in the jobs/ directory with a main() function
        that will be called from run_chain().
    force : bool
        If True will do job regardless of completion status
    """

    for time in tools.iter_hours(start, hstart, hstop, cfg.restart_step):
        print(time)
        if time == start:
            setattr(cfg, "first_one", True)
            setattr(cfg, "second_one", False)
            run_time = min(cfg.restart_step, hstop - hstart)
            print('First simulation')
        elif time == start + timedelta(hours=cfg.restart_step):
            setattr(cfg, "first_one", False)
            setattr(cfg, "second_one", True)
            run_time = min(cfg.restart_step + cfg.spinup, hstop - hstart)
            print('Second simulation')
        else:
            setattr(cfg, "first_one", False)
            setattr(cfg, "second_one", False)
            run_time = min(cfg.restart_step + cfg.spinup, hstop - hstart)

        if run_time == 0:
            # don't start simuation with 0 runtime
            continue

        endtime_act_sim = time - timedelta(hours=cfg.restart_step) \
                               + timedelta(hours=run_time) 
        if endtime_act_sim > start + timedelta(hours=hstop):
            continue

        print('Runtime of sub-simulation: ', run_time)
 
        if cfg.first_one:
            run_chain(work_root = work_root,
                      cfg = cfg,
                      start_time = time,
                      hstart = 0, 
                      hstop = run_time,
                      job_names = job_names,
                      force = force)
        else:
            run_chain(work_root = work_root,
                      cfg = cfg,
                      start_time = time,
                      hstart = -cfg.spinup, 
                      hstop = run_time - cfg.spinup,
                      job_names = job_names,
                      force = force)


if __name__ == '__main__':
    args = parse_arguments()

    # 'empty' config object to be overwritten by load_config_file
    cfg = None
    for casename in args.casenames:
        cfg = load_config_file(casename=casename, cfg=cfg)
        start_time = datetime.strptime(args.startdate, '%Y-%m-%d')
        set_simulation_type(cfg)
        if args.job_list is None:
            args.job_list = default_jobs[cfg.target]

        print("Starting chain for case {}, using {}".format(casename,
                                                            cfg.target.name))

        if cfg.target is tools.Target.COSMO:
            if cfg.target.subtarget is tools.Subtarget.NONE:
                restart_runs(work_root = cfg.work_root,
                             cfg = cfg,
                             start = start_time,
                             hstart = args.hstart,
                             hstop = args.hstop,
                             job_names = args.job_list,
                             force = args.force)
            elif cfg.target.subtarget is tools.Subtarget.SPINUP:
                restart_runs_spinup(work_root = cfg.work_root,
                                    cfg = cfg,
                                    start = start_time,
                                    hstart = args.hstart,
                                    hstop = args.hstop,
                                    job_names = args.job_list,
                                    force = args.force)
            else:
                raise RuntimeError("Unknown subtarget: {}".format(cfg.subtarget))
        elif cfg.target is tools.Target.COSMOART:
            # cosmoart can't do restarts
            run_chain(   work_root = cfg.work_root,
                         cfg = cfg,
                         start_time = start_time,
                         hstart = args.hstart,
                         hstop = args.hstop,
                         job_names = args.job_list,
                         force = args.force)
        else:
            raise RuntimeError("Unknown target: {}".format(cfg.target))

    print('>>> finished chain for good or bad! <<<')
