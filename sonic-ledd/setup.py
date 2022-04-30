from setuptools import setup

setup(
    name='sonic-ledd',
    version='1.1',
    description='Front-panel LED control daemon for SONiC',
    license='Apache 2.0',
    author='SONiC Team',
    author_email='linuxnetdev@microsoft.com',
    url='https://github.com/Azure/sonic-platform-daemons',
    maintainer='Joe LeVeque',
    maintainer_email='jolevequ@microsoft.com',
    scripts=[
        'scripts/ledd',
    ],
    setup_requires=[
        'wheel'
    ],
    tests_require=[
        'mock>=2.0.0; python_version < "3.3"',
        'pytest',
        'pytest-cov'
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
        'Topic :: System :: Hardware',
    ],
    keywords='sonic SONiC LED led daemon LEDD ledd',
)
