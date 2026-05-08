from setuptools import setup

setup(
    name='sonic-bmcctld',
    version='1.0',
    description='BMC controller daemon for SONiC',
    license='Apache 2.0',
    author='SONiC Team',
    author_email='linuxnetdev@microsoft.com',
    url='https://github.com/Azure/sonic-platform-daemons',
    maintainer='Judy Joseph',
    maintainer_email='judyjoseph@microsoft.com',
    packages=[
        'tests'
    ],
    scripts=[
        'scripts/bmcctld',
    ],
    setup_requires=[
        'pytest-runner',
        'wheel'
    ],
    tests_require=[
        'pytest',
        'mock>=2.0.0',
        'pytest-cov',
        'sonic-platform-common'
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
        'Programming Language :: Python :: 3.7',
        'Topic :: System :: Hardware',
    ],
    keywords='sonic SONiC BMC bmc controller bmcctld',
    test_suite='setup.get_test_suite'
)
