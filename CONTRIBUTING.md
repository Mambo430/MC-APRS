# CONTRIBUTING

Code contributions are welcomed and appreciated. Just submit a PR!

The current build environment uses `pre-commit`, and `hatch`.

### Environment setup:

```console
pip install hatch
git clone https://github.com/Mambo430/MC-APRS.git
cd MC-APRS
pre-commit install

# Optionally run the pre-commit scripts at any time
pre-commit run --all-files
```

### Running and testing:

From the MC-APRS checkout directory:

```console
hatch shell

# Running
python -m mc_aprs


# Testing
hatch test
```
