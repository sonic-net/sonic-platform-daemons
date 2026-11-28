from setuptools import setup, find_packages


TEST_REQUIREMENTS = [
    'pytest>=7',
    'pytest-cov',
    # This fork revision fixes protobuf imports and supports SONiC's gRPC versions.
    'xcvr-emu @ git+https://github.com/az-pz/xcvr-emu.git@3aca04f89de6dfecf33bea29509234164d917a81',
]


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
    install_requires=[
        # NOTE: This package also requires swsscommon, but it is not currently installed as a wheel
        'enum34; python_version < "3.4"',
        'sonic-py-common',
    ],
    setup_requires=[
        'wheel'
    ],
    tests_require=TEST_REQUIREMENTS,
    extras_require={
        'testing': TEST_REQUIREMENTS,
    },
    classifiers=[
        'Development Status :: 4 - Beta',
        'Environment :: No Input/Output (Daemon)',
        'Intended Audience :: Developers',
        'Intended Audience :: Information Technology',
        'Intended Audience :: System Administrators',
        'License :: OSI Approved :: Apache Software License',
        'Natural Language :: English',
        'Operating System :: POSIX :: Linux',
        'Programming Language :: Python :: 3.7',
        'Topic :: System :: Hardware',
    ],
    keywords='sonic SONiC TRANSCEIVER transceiver daemon XCVRD xcvrd',
)
