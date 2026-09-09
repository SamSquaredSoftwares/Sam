"""Keep the two dependency manifests in step.

`requirements.txt` is the local virtualenv that `run_local.sh` and the tests
use; `actions/package.yaml` is the RCC-managed environment the Action Server
builds in production. Nothing installs both, so a package pinned differently
in each works locally and breaks in production, or the reverse -- and that
gap has already produced a real defect here: package.yaml pinned
`anthropic=0.121.0` while `requirements.txt` required `>=1,<2`, so the
httpx/httpx2 handling in `actions/tls_trust.py` ran against an SDK major it
was never tested on.
"""

import re
from pathlib import Path

import pytest
import yaml
from packaging.requirements import Requirement
from packaging.version import Version

ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS = ROOT / "requirements.txt"
PACKAGE_YAML = ROOT / "actions" / "package.yaml"

# package.yaml uses conda-style `name=version`, which is not PEP 508.
CONDA_PIN = re.compile(r"^(?P<name>[A-Za-z0-9._-]+)\s*=\s*(?P<version>[A-Za-z0-9.!+-]+)$")


def requirements() -> dict[str, Requirement]:
    found = {}
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            requirement = Requirement(line)
            found[requirement.name.lower()] = requirement
    return found


def package_pins() -> dict[str, str]:
    spec = yaml.safe_load(PACKAGE_YAML.read_text(encoding="utf-8"))
    pins = {}
    for entry in spec["dependencies"].get("pypi") or []:
        match = CONDA_PIN.match(entry.strip())
        assert match, f"unparseable pypi pin in package.yaml: {entry!r}"
        pins[match["name"].lower()] = match["version"]
    return pins


SHARED = sorted(set(requirements()) & set(package_pins()))


def test_the_manifests_actually_overlap():
    # If this ever empties out, every test below would vacuously pass.
    assert SHARED, "the two manifests share no packages; the comparison is meaningless"


@pytest.mark.parametrize("name", SHARED)
def test_package_yaml_pin_satisfies_requirements_txt(name):
    pinned = Version(package_pins()[name])
    declared = requirements()[name].specifier
    assert pinned in declared, (
        f"actions/package.yaml pins {name}={pinned}, which does not satisfy "
        f"requirements.txt's {name}{declared}. Production and local would run "
        f"different versions of {name}."
    )


def test_the_anthropic_sdk_is_1x_in_both():
    # tls_trust builds the client on the SDK's httpx2 boundary, which is 1.x
    # only. Pinning either manifest below 1.0 silently disables custom CA
    # bundles on the Anthropic path -- the defect CLAUDE.md documents.
    assert Version(package_pins()["anthropic"]) >= Version("1.0")
    assert Version("1.0") in requirements()["anthropic"].specifier
