#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Setup the namelist for a COSMO tracer run and submit the job to the queue
#
# result in case of success: forecast fields found in  
#                            ${cosmo_output}
#
# Dominik Brunner, July 2013
#
# 2013-07-21 Initial release, adopted from Christoph Knote's cosmo.bash (brd)
# 2018-07-10 Translated to Python (muq)

# Not tested yet. Note the comment.

### DEVELOPMENT VERSION ###

import logging
import os
import shutil 
from subprocess import call
import sys
from . import tools
import importlib
import subprocess


def main(starttime, hstart, hstop, cfg):
    """
    Setup the namelist for a COSMO tracer run and submit the job to the queue
    """
    logfile=os.path.join(cfg.log_working_dir,"cosmo")
    logfile_finish=os.path.join(cfg.log_finished_dir,"cosmo")
    tools.change_logfile(logfile)

    logging.info('Setup the namelist for a COSMO tracer run and submit the job to the queue')

    np_io= 0 ;     setattr(cfg,"np_io",np_io)


# Set number of nodes and cores for COSMO 
    if cfg.compute_queue=="normal":
        walltime="08:00:00"
        np_x=5
        np_y=4
    elif cfg.compute_queue=="debug":
        walltime="00:30:00"
        np_x=1
        np_y=1
        ppn=1    
    else: 
        logging.error("unsetted queueName %s" %cfg.compute_queue)
        sys.exit(1)

    np_tot = np_x * np_y + np_io     

    setattr(cfg,"np_x",np_x)
    setattr(cfg,"np_y",np_y)
    setattr(cfg,"np_tot",np_tot)
    setattr(cfg,"walltime",walltime)

# change of soil model from TERRA to TERRA multi-layer on 2 Aug 2007
    if int(starttime.strftime("%Y%m%d%H")) < 2007080200:   #input starttime as a number
        multi_layer=".FALSE."
    else:
        multi_layer=".TRUE."
    setattr(cfg,"multi_layer",multi_layer)

# create directory
    try:
        os.makedirs(cfg.cosmo_work, exist_ok=True)
    except (OSError, PermissionError):
        logging.error("Creating cosmo_work folder failed")
        raise
  
    try:
        os.makedirs(cfg.cosmo_output, exist_ok=True)   #output_root not used in cfg
    except (OSError, PermissionError):
        logging.error("Creating cosmo_output folder failed")
        raise

    try:
        os.makedirs(cfg.cosmo_restart_out, exist_ok=True)   #can't find this root in cfg. Use a temporary name here.
    except (OSError, PermissionError):
        logging.error("Creating cosmo_restart_out folder failed")
        raise
    
# copy cosmo.exe
    try:
        # 'cosmo' file name or directory
        shutil.copy(cfg.cosmo_bin, os.path.join(cfg.cosmo_work,'cosmo'))
    except FileNotFoundError:
        logging.error("cosmo_bin not found")
        raise
    except (PermissionError, OSError):
        logging.error("Copying cosmo_bin failed")
        raise

# Write INPUT_BGC from csv file
    # csv file with tracer definitions 
    tracer_csvfile = os.path.join(cfg.casename,'cosmo_tracers.csv')

    tracer_filename = os.path.join(cfg.chain_src_dir,'cases',tracer_csvfile)
    input_bgc_filename = os.path.join(cfg.cosmo_work,'INPUT_BGC')

    tools.write_cosmo_input_bgc.main(tracer_filename,input_bgc_filename)
# Prepare namelist and submit job
    sys.path.append(os.path.dirname(cfg.cosmo_namelist))
    input_script = importlib.import_module(os.path.basename(cfg.cosmo_namelist))
    input_script.main(cfg)

    sys.path.append(os.path.dirname(cfg.cosmo_runjob))
    input_script = importlib.import_module(os.path.basename(cfg.cosmo_runjob))
    input_script.main(cfg,logfile,logfile_finish)

    subprocess.call(["sbatch", "--wait", os.path.join(cfg.cosmo_work,'run.job')])

