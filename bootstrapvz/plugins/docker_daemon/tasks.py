from bootstrapvz.base import Task
from bootstrapvz.common import phases
from bootstrapvz.common.tasks import grub
from bootstrapvz.common.tasks import initd
from bootstrapvz.common.tools import log_check_call
from bootstrapvz.common.tools import sed_i
from bootstrapvz.providers.gce.tasks import boot as gceboot
from distutils.version import LooseVersion
import os
import os.path
import shutil
import subprocess
import tempfile
import time

ASSETS_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), 'assets'))


class AddDockerDeps(Task):
	description = 'Add packages for docker deps'
	phase = phases.package_installation
	DOCKER_DEPS = ['aufs-tools', 'btrfs-tools', 'git', 'iptables',
	               'procps', 'xz-utils', 'ca-certificates']

	@classmethod
	def run(cls, info):
		for pkg in cls.DOCKER_DEPS:
			info.packages.add(pkg)


class AddDockerBinary(Task):
	description = 'Add docker binary'
	phase = phases.system_modification

	@classmethod
	def run(cls, info):
		docker_version = LooseVersion(info.manifest.plugins['docker_daemon'].get('version', 'latest'))
		filename = 'docker-' + str(docker_version)
		# Docker versions > 1.10 are released in a compressed tarball,
		# and so it needs to be treated diferently.
		if docker_version > '1.10':
			cls._run_new_versions(info, filename)
		else:
			cls._run_old_versions(info, filename)

	@classmethod
	def _run_new_versions(cls, info, filename):
		filename += '.tgz'
		url = cls._get_url(filename)
		filepath = os.path.join(info.root, filename)
		try:
			cls._download_file(url, filepath)
			extraction_path = tempfile.mkdtemp(prefix='docker-extration-')
			cls._extract_files(filepath, extraction_path)
			bin_path = cls._get_bin_path(info)
			cls._install_docker_files(extraction_path, bin_path)
		finally:
			try:
				os.remove(filepath)
			except OSError:
				pass
			shutil.rmtree(extraction_path, ignore_errors=True)

	@classmethod
	def _run_old_versions(cls, info, filename):
		url = cls._get_url(filename)
		bin_docker = os.path.join(cls._get_bin_path(info), 'docker')
		cls._download_file(url, bin_docker)
		cls._make_executable(bin_docker)

	@classmethod
	def _get_url(cls, filename):
		return 'https://get.docker.io/builds/Linux/x86_64/' + filename

	@classmethod
	def _download_file(cls, url, dest_path):
		log_check_call(['wget', '-O', dest_path, url])

	@classmethod
	def _extract_files(cls, filepath, extraction_path):
		log_check_call(['tar', 'xf', filepath, '-C', extraction_path])

	@classmethod
	def _get_bin_path(cls, info):
		return os.path.join(info.root, 'usr/bin')

	@classmethod
	def _install_docker_files(cls, source_path, dest_path):
		for root, dirs, files in os.walk(source_path, topdown=False):
			for file in files:
				full_filename = os.path.join(root, file)
				cls._make_executable(full_filename)
				shutil.move(full_filename, os.path.join(dest_path, file))

	@classmethod
	def _make_executable(cls, filepath):
		os.chmod(filepath, 0755)


class AddDockerInit(Task):
	description = 'Add docker init script'
	phase = phases.system_modification
	successors = [initd.InstallInitScripts]

	@classmethod
	def run(cls, info):
		init_src = os.path.join(ASSETS_DIR, 'init.d/docker')
		info.initd['install']['docker'] = init_src
		default_src = os.path.join(ASSETS_DIR, 'default/docker')
		default_dest = os.path.join(info.root, 'etc/default/docker')
		shutil.copy(default_src, default_dest)
		docker_opts = info.manifest.plugins['docker_daemon'].get('docker_opts')
		if docker_opts:
			sed_i(default_dest, r'^#*DOCKER_OPTS=.*$', 'DOCKER_OPTS="%s"' % docker_opts)


class EnableMemoryCgroup(Task):
	description = 'Change grub configuration to enable the memory cgroup'
	phase = phases.system_modification
	successors = [grub.InstallGrub_1_99, grub.InstallGrub_2]
	predecessors = [grub.ConfigureGrub, gceboot.ConfigureGrub]

	@classmethod
	def run(cls, info):
		grub_config = os.path.join(info.root, 'etc/default/grub')
		sed_i(grub_config, r'^(GRUB_CMDLINE_LINUX*=".*)"\s*$', r'\1 cgroup_enable=memory"')


class PullDockerImages(Task):
	description = 'Pull docker images'
	phase = phases.system_modification
	predecessors = [AddDockerBinary]

	@classmethod
	def run(cls, info):
		from bootstrapvz.common.exceptions import TaskError
		from subprocess import CalledProcessError
		images = info.manifest.plugins['docker_daemon'].get('pull_images', [])
		retries = info.manifest.plugins['docker_daemon'].get('pull_images_retries', 10)

		bin_docker = os.path.join(info.root, 'usr/bin/docker')
		graph_dir = os.path.join(info.root, 'var/lib/docker')
		socket = 'unix://' + os.path.join(info.workspace, 'docker.sock')
		pidfile = os.path.join(info.workspace, 'docker.pid')

		try:
			# start docker daemon temporarly.
			daemon = subprocess.Popen([bin_docker, '-d', '--graph', graph_dir, '-H', socket, '-p', pidfile])
			# wait for docker daemon to start.
			for _ in range(retries):
				try:
					log_check_call([bin_docker, '-H', socket, 'version'])
					break
				except CalledProcessError:
					time.sleep(1)
			for img in images:
				# docker load if tarball.
				if img.endswith('.tar.gz') or img.endswith('.tgz'):
					cmd = [bin_docker, '-H', socket, 'load', '-i', img]
					try:
						log_check_call(cmd)
					except CalledProcessError as e:
						msg = 'error {e} loading docker image {img}.'.format(img=img, e=e)
						raise TaskError(msg)
				# docker pull if image name.
				else:
					cmd = [bin_docker, '-H', socket, 'pull', img]
					try:
						log_check_call(cmd)
					except CalledProcessError as e:
						msg = 'error {e} pulling docker image {img}.'.format(img=img, e=e)
						raise TaskError(msg)
		finally:
			# shutdown docker daemon.
			daemon.terminate()
			os.remove(os.path.join(info.workspace, 'docker.sock'))
