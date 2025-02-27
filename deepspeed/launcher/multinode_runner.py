import base64
import json
import os
import sys
import shutil
import subprocess
import warnings
from abc import ABC, abstractmethod

from ..utils import logger
from .constants import PDSH_MAX_FAN_OUT, MVAPICH_TMP_HOSTFILE


class MultiNodeRunner(ABC):
    def __init__(self, args, world_info_base64):
        self.args = args
        self.user_arguments = self.parse_user_args()
        self.user_script = args.user_script
        self.world_info_base64 = world_info_base64
        self.exports = {}

    @abstractmethod
    def backend_exists(self):
        pass

    @abstractmethod
    def get_cmd(self, environment, active_resources):
        pass

    def add_export(self, key, var):
        self.exports[key.strip()] = var.strip()

    def parse_user_args(self):
        return self.args.user_args


class PDSHRunner(MultiNodeRunner):
    def __init__(self, args, world_info_base64):
        super().__init__(args, world_info_base64)

    def backend_exists(self):
        return shutil.which('pdsh')

    def parse_user_args(self):
        return list(
            map(lambda x: x if x.startswith("-") else "'{}'".format(x),
                self.args.user_args))

    def get_cmd(self, environment, active_resources):
        environment['PDSH_RCMD_TYPE'] = 'ssh'

        active_workers = ",".join(active_resources.keys())
        logger.info("Running on the following workers: %s" % active_workers)

        # PDSH flags for max node fan out and specific hosts to launch on
        # See https://linux.die.net/man/1/pdsh for flag details
        pdsh_cmd_args = ['pdsh', '-f', str(PDSH_MAX_FAN_OUT), '-w', active_workers]

        exports = ""
        for key, val in self.exports.items():
            exports += "export {}={}; ".format(key, val)

        deepspeed_launch = [
            exports,
            "cd {};".format(os.path.abspath('.')),
            sys.executable,
            "-u",
            "-m",
            "deepspeed.launcher.launch",
            '--world_info={}'.format(self.world_info_base64),
            "--node_rank=%n",
            "--master_addr={}".format(self.args.master_addr),
            "--master_port={}".format(self.args.master_port)
        ]
        if self.args.detect_nvlink_pairs:
            deepspeed_launch += ["--detect_nvlink_pairs"]

        return pdsh_cmd_args + deepspeed_launch + [self.user_script
                                                   ] + self.user_arguments


class OpenMPIRunner(MultiNodeRunner):
    def __init__(self, args, world_info_base64, resource_pool):
        super().__init__(args, world_info_base64)
        self.resource_pool = resource_pool
        self.add_export('UCX_TLS', 'tcp')

    def backend_exists(self):
        #TODO: if IB is available we should suggestion mvapich
        return shutil.which('ompi_info')

    def get_cmd(self, environment, active_resources):
        #TODO: Allow for include/exclude at node-level but not gpu-level
        assert self.args.include == "" and self.args.exclude == "", 'openmpi backend does not support worker include/exclusion'
        assert self.args.num_nodes == -1 and self.args.num_gpus == -1, 'openmpi backend does not support limiting num nodes/gpus'
        assert not self.args.detect_nvlink_pairs, "openmpi backend does not support remapping visible devices"
        total_process_count = sum(self.resource_pool.values())
        allow_run_as_root = os.environ.get('RUN_MPI_AS_ROOT', False)
        mpirun_cmd = [
            'mpirun',
            '-n',
            f'{total_process_count}',
            '-hostfile',
            f'{self.args.hostfile}',
            '--mca',
            'btl',
            '^openib',
            '--mca',
            'btl_tcp_if_include',
            'eth0',
        ]
        if allow_run_as_root:
            mpirun_cmd.insert(1, '--allow-run-as-root')
            
        export_cmd = []
        for k, v in self.exports.items():
            export_cmd += ['-x', f'{k}={v}']

        python_exec = [sys.executable, "-u"]

        return mpirun_cmd + export_cmd + python_exec + [self.user_script
                                                        ] + self.user_arguments

class SlurmRunner(MultiNodeRunner):
    def __init__(self, args, world_info_base64, resource_pool):
        super().__init__(args, world_info_base64)
        self.resource_pool = resource_pool

    def backend_exists(self):
        return shutil.which('sinfo')
    
    def parse_user_args(self):
        user_args = []
        for arg in self.args.user_args:
            if arg.startswith('{') and arg.endswith('}'):
                try:
                    arg_dict = json.loads(arg)
                    if 'config_files' in arg_dict:
                        config_files = {}
                        for k, v in arg_dict.get('config_files', {}).items():
                            config_files[k] = json.loads(v)
                        arg_dict['config_files'] = config_files
                except json.JSONDecodeError as jde:
                    raise ValueError('SLURM is picky and needs you to use plain json for your configs. Check for comments and lowercase trues') from jde
                arg = json.dumps(arg_dict, separators=(',', ':'))
            user_args.append(arg)
        return user_args

    def get_cmd(self, environment, active_resources):
        assert not getattr(self.args, 'detect_nvlink_pairs', False), "slurm backend does not support remapping visible devices"
        total_process_count = sum(self.resource_pool.values())
        srun_cmd = [
            'srun',
            '-n',
            f'{total_process_count}',
        ]
        if self.args.comment != '':
            srun_cmd += ['--comment', self.args.comment]

        if self.args.include != "":
            srun_cmd.append('--include')
            srun_cmd.append(f'{self.args.include}')
        if self.args.exclude != "":
            srun_cmd.append('--exclude')
            srun_cmd.append(f'{self.args.exclude}')
        if self.args.num_nodes > 0:
            srun_cmd.append('--nodes')
            srun_cmd.append(f'{self.args.num_nodes}')
        if self.args.num_gpus > 0:
            srun_cmd.append('--gpus')
            srun_cmd.append(f'{self.args.num_gpus}')

        exports = '--export=ALL' 
        for key, val in self.exports.items():
            exports +=  f",{key}={val}" 

        python_exec = [sys.executable, "-u"]
        command = srun_cmd + [exports] + python_exec + [self.user_script] + self.user_arguments
        return command


class MVAPICHRunner(MultiNodeRunner):
    def __init__(self, args, world_info_base64, resource_pool):
        super().__init__(args, world_info_base64)
        self.resource_pool = resource_pool

        # Disable the CMA kernel module, not available on Ubuntu systems
        self.add_export('MV2_SMP_USE_CMA', '0')

        # If we fail this will output more verbose logging
        self.add_export('MV2_DEBUG_SHOW_BACKTRACE', '1')

        # Enabled cuda-aware communication
        self.add_export('MV2_USE_CUDA', '1')

        # Support deep learning frameworks: http://hidl.cse.ohio-state.edu/userguide/horovod/
        self.add_export('MV2_SUPPORT_DL', '1')

        # Support MPI_THREAD_MULTIPLE
        self.add_export('MV2_ENABLE_AFFINITY', '0')

        # Performance tuning flags for allgather
        self.add_export('MV2_INTER_ALLGATHER_TUNING', '5')
        self.add_export('MV2_CUDA_USE_NAIVE', '0')

    def backend_exists(self):
        #TODO: if IB is available we should suggestion mvapich
        mpiname_exists = shutil.which('mpiname')
        exists = False
        if not mpiname_exists:
            warnings.warn("mpiname does not exist, mvapich is not installed properly")
        else:
            results = subprocess.check_output('mpiname', shell=True)
            mpiname_results = results.decode('utf-8').strip()
            if "MVAPICH2-GDR" in mpiname_results:
                exists = True
            else:
                warnings.warn(
                    f"Expected MVAPICH2-GDR as return for mpiname but received {mpiname_results}"
                )
        return exists

    def get_cmd(self, environment, active_resources):
        #TODO: Allow for include/exclude at node-level but not gpu-level
        assert self.args.include == "" and self.args.exclude == "", 'mvapich backend does not support worker include/exclusion'
        assert self.args.num_nodes == -1 and self.args.num_gpus == -1, 'mvapich backend does not support limiting num nodes/gpus'
        assert not self.args.detect_nvlink_pairs, "openmpi backend does not support remapping visible devices"

        devices_per_node = self.resource_pool.values()
        total_process_count = sum(devices_per_node)
        process_per_node = list(devices_per_node)[0]
        assert all([n == process_per_node for n in devices_per_node]), "mvapich requires same number of devices per node"

        with open(MVAPICH_TMP_HOSTFILE, 'w') as fd:
            for host in self.resource_pool.keys():
                fd.write(f'{host}\n')

        mpirun_cmd = [
            'mpirun',
            '-np',
            f'{total_process_count}',
            '-ppn',
            f'{process_per_node}',
            '--hostfile',
            f'{MVAPICH_TMP_HOSTFILE}',
        ]

        export_cmd = []
        for k, v in self.exports.items():
            export_cmd += ['-env', f'{k}={v}']

        python_exec = [sys.executable, "-u"]

        return mpirun_cmd + export_cmd + python_exec + [self.user_script
                                                        ] + self.user_arguments
class MosaicMLRunner(MultiNodeRunner):
    def __init__(self, args, world_info_base64):
        super().__init__(args, world_info_base64)

    def backend_exists(self):
        return True

    def parse_user_args(self):
        user_args = []
        for arg in self.args.user_args:
            if arg.startswith('{') and arg.endswith('}'):
                try:
                    arg_dict = json.loads(arg)
                    if 'config_files' in arg_dict:
                        config_files = {}
                        for k, v in arg_dict.get('config_files', {}).items():
                            config_files[k] = json.loads(v)
                        arg_dict['config_files'] = config_files
                except json.JSONDecodeError as jde:
                    raise ValueError('Please use plain json for your configs. Check for comments and lowercase trues') from jde
                arg = json.dumps(arg_dict, separators=(',', ':'))
            user_args.append(arg)
        return user_args

    def get_cmd(self, environment, active_resources):
        deepspeed_launch = [
            sys.executable,
            "-u",
            "-m",
            "deepspeed.launcher.launch",
            '--world_info={}'.format(self.world_info_base64),
            "--node_rank={}".format(os.environ['NODE_RANK']),
            "--master_addr={}".format(os.environ['MASTER_ADDR']),
            "--master_port={}".format(os.environ['MASTER_PORT']),
        ]

        return deepspeed_launch + [self.user_script] + self.user_arguments
