from setuptools import setup, find_packages
import os
import os.path
from distutils.command.build_py import build_py as _build_py
import distutils.command
import pkg_resources
#import subprocess
#proto_files = [os.path.join(os.path.dirname(os.path.abspath(__file__)),'proto', 'image_transform.proto')]
#proto_files = ['proto/image_transform.proto']

class GrpcTool(distutils.cmd.Command):
    def initialize_options(self):
        pass

    def finalize_options(self):
        pass

    def run(self):
        import grpc_tools.protoc

        proto_include = pkg_resources.resource_filename('proto', '')

        grpc_tools.protoc.main([
            'grpc_tools.protoc',
            '-I{}'.format(proto_include),
            '--python_out=proto_out/',
            '--grpc_python_out=proto_out/',
            'linkmgr_grpc_driver.proto'
        ])

class BuildPyCommand (_build_py, object):
    def run(self):
        self.run_command('GrpcTool')
        super(BuildPyCommand, self).run()

setup(
    name='sonic-xcvrd',
    version='1.0',
    description='Transceiver monitoring daemon for SONiC',
    license='Apache 2.0',
    author='SONiC Team',
    author_email='linuxnetdev@microsoft.com',
    url='https://github.com/Azure/sonic-platform-daemons',
    maintainer='Kebo Liu',
    maintainer_email='kebol@mellanox.com',
    packages=find_packages(),
    entry_points={
        'console_scripts': [
            'xcvrd = xcvrd.xcvrd:main',
        ]
    },
    cmdclass={'build_py': BuildPyCommand,
              'GrpcTool': GrpcTool},
    install_requires=[
        # NOTE: This package also requires swsscommon, but it is not currently installed as a wheel
        'enum34; python_version < "3.4"',
        'sonic-py-common',
    ],
    setup_requires=[
        'wheel',
        'grpcio-tools'
    ],
    tests_require=[
        'pytest',
        'pytest-cov',
    ],
    classifiers=[
        'Development Status :: 4 - Beta',
        'Environment :: No Input/Output (Daemon)',
        'Intended Audience :: Developers',
        'Intended Audience :: Information Technology',
        'Intended Audience :: System Administrators',
        'License :: OSI Approved :: Apache Software License',
        'Natural Language :: English',
        'Operating System :: POSIX :: Linux',
        'Programming Language :: Python :: 2.7',
        'Programming Language :: Python :: 3.7',
        'Topic :: System :: Hardware',
    ],
    keywords='sonic SONiC TRANSCEIVER transceiver daemon XCVRD xcvrd',
)

"""for file in proto_files:
    print("grpc generation result for '{}'".format(file))
    args = "--proto_path=. --python_out=. --grpc_python_out=. {0}".format(file)
    result = subprocess.call("python3 -m grpc_tools.protoc " + args, shell=True)
    print("grpc generation result for '{0}': code {1}".format(file, result))
    """

