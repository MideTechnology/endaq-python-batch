import setuptools

with open('README.rst', 'r', encoding='utf-8') as fh:
    long_description = fh.read()

INSTALL_REQUIRES = [
    "numpy",
    "scipy",
    "pandas",
    "idelib>=3.1.0",
    "backports.cached-property; python_version<'3.8'",
    ]

TEST_REQUIRES = [
    "pytest",
    "pytest-cov",
    "hypothesis",
    "sympy",
    "numpy-quaternion==2020.11.2.17.0.49",
    "plotly",
    ]

EXAMPLE_REQUIRES = [
    ]

setuptools.setup(
        name='endaq-batch',
        version='1.0.0a1',
        author='Mide Technology',
        author_email='help@mide.com',
        description='A comprehensive, user-centric Python API for working with enDAQ data and devices',
        long_description=long_description,
        long_description_content_type='text/markdown',
        url='https://github.com/MideTechnology/endaq-python',
        license='MIT',
        classifiers=['Development Status :: 2 - Pre-Alpha',
                     'License :: OSI Approved :: MIT License',
                     'Natural Language :: English',
                     'Programming Language :: Python :: 3.5',
                     'Programming Language :: Python :: 3.6',
                     'Programming Language :: Python :: 3.7',
                     'Programming Language :: Python :: 3.8',
                     'Programming Language :: Python :: 3.9',
                     'Topic :: Scientific/Engineering',
                     ],
        keywords='ebml binary ide mide endaq',
        packages=['endaq.batch'],
        package_dir={'endaq.batch': './endaq/batch'},
        install_requires=INSTALL_REQUIRES,
        extras_require={
            'test': INSTALL_REQUIRES + TEST_REQUIRES,
            'example': INSTALL_REQUIRES + EXAMPLE_REQUIRES,
            },
)
