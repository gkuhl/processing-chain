#!/usr/bin/env python
# -*- coding: utf-8 -*-

# Copy cosmo output from scratch to store (or anywhere else)

### DEVELOPMENT VERSION ###

import logging
import os
import shutil
import datetime
import glob 
from subprocess import call
import sys
from . import tools


def logfile_header_template():
    """Returns a template for the logfile-header"""
    return (
    "\n=====================================================\n"
    "============== POST PROCESSING {}\n"
    "============== {}\n"
    "=====================================================\n\n"
    )


def runscript_header_template():
    """Returns a template for the runscript-header (#SBATCH-directives)"""
    return '\n'.join(
        ["#SBATCH --job-name=post_cosmo",
         "#SBATCH --nodes=1",
         "#SBATCH --partition=xfer",
         "#SBATCH --constraint=gpu",
         "#SBATCH --account={compute_account}",
         "#SBATCH --output={logfile}",
         "#SBATCH --open-mode=append",
         "#SBATCH --workdir={cosmo_work}",
         ""])


def runscript_commands_template():
    """Return a template for the commands in the runscript"""
    commands = list()
    
    return '\n'.join(
        ["mkdir -p {target_dir}",
         "cp -R {int2lm_work_src} {int2lm_work_dest}",
         "cp -R {cosmo_work_src} {cosmo_work_dest}",
         "cp -R {cosmo_output_src} {cosmo_output_dest}",
         "cp -R {logs_src} {logs_dest}"])


def main(starttime, hstart, hstop, cfg):
    """Copy the output of a **COSMO**-run to a user-defined position.

    Write a runscript to copy all files (**COSMO** settings & output,
    **int2lm** settings, logfiles) from ``cfg.cosmo_work``,
    ``cfg.cosmo_output``, ``cfg.int2lm_work``, ``cfg.log_finished_dir`` to
    ``cfg.output_root/...`` .
    
    Submit the job to the xfer-queue.
    
    Parameters
    ----------	
    start_time : datetime-object
        The starting date of the simulation
    hstart : int
        Offset (in hours) of the actual start from the start_time
    hstop : int
        Length of simulation (in hours)
    cfg : config-object
        Object holding all user-configuration parameters as attributes
    """
    if cfg.compute_host!="daint":
        logging.error("The copy script is supposed to be run on daint only,"
                      "not on {}".format(cfg.compute_host))
        raise RuntimeError("Wrong compute host for copy-script")

    logfile=os.path.join(cfg.log_working_dir,"post_cosmo")
    cosmo_work_dir = cfg.cosmo_work
    runscript_path = os.path.join(cfg.cosmo_work, "cp_cosmo.job")
    copy_path = os.path.join(cfg.output_root, starttime.strftime('%Y%m%d%H')+
                             "_"+str(int(hstart))+"_"+str(int(hstop)))

    logging.info(logfile_header_template()
                 .format("STARTS",
                         str(datetime.datetime.today())))

    tools.create_dir(copy_path, "output")

    runscript_content = "#!/bin/bash\n"
    runscript_content += runscript_header_template().format(
        compute_account = cfg.compute_account,
        logfile = logfile,
        cosmo_work = cfg.cosmo_work)
    runscript_content += runscript_commands_template().format(
        target_dir = copy_path,
        int2lm_work_src = cfg.int2lm_work,
        int2lm_work_dest = os.path.join(copy_path, "int2lm_run"),
        cosmo_work_src = cfg.cosmo_work,
        cosmo_work_dest = os.path.join(copy_path, "cosmo_run"),
        cosmo_output_src = cfg.cosmo_output,
        cosmo_output_dest = os.path.join(copy_path, "cosmo_output"),
        logs_src = cfg.log_finished_dir,
        logs_dest = os.path.join(copy_path, "logs"),)

    with open(runscript_path, "w") as script:
        script.write(runscript_content)

    call(["sbatch","--wait" ,runscript_path])

    logging.info(logfile_header_template()
                 .format("ENDS",
                         str(datetime.datetime.today())))

    # copy own logfile aswell
    tools.copy_file(logfile, os.path.join(copy_path, "logs/"))
