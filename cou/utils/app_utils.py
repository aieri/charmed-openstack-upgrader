# Copyright 2023 Canonical Limited
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Application utilities."""
import json
import logging
from typing import Iterable, Optional

from cou.exceptions import RunUpgradeError
from cou.utils.juju_utils import Model, Unit
from cou.utils.openstack import CEPH_RELEASES

logger = logging.getLogger(__name__)


async def upgrade_packages(unit: str, model: Model, packages_to_hold: Optional[list]) -> None:
    """Run package updates and upgrades on each unit of an Application.

    :param unit: Unit name where the package upgrade runs on.
    :type unit: str
    :param model: Model object
    :type model: Model
    :param packages_to_hold: A list of packages to put on hold during package upgrade.
    :type packages_to_hold: Optional[list]
    :raises CommandRunFailed: When a command fails to run.
    """
    dpkg_opts = "-o Dpkg::Options::=--force-confnew -o Dpkg::Options::=--force-confdef"
    command = f"apt-get update && apt-get dist-upgrade {dpkg_opts} -y && apt-get autoremove -y"
    if packages_to_hold:
        packages = " ".join(packages_to_hold)
        command = f"apt-mark hold {packages} && {command} ; apt-mark unhold {packages}"

    await model.run_on_unit(unit_name=unit, command=command, timeout=600)


async def set_require_osd_release_option(unit: str, model: Model) -> None:
    """Check and set the correct value for require-osd-release on a ceph-mon unit.

    This function compares the value of require-osd-release option with the current release
    of OSDs. If they are not the same, set the OSDs release as the value for
    require-osd-release.
    :param unit: The ceph-mon unit name where the check command runs on.
    :type unit: str
    :param model: Model object
    :type model: Model
    :raises CommandRunFailed: When a command fails to run.
    """
    # The current `require_osd_release` value set on the ceph-mon unit
    current_require_osd_release = await _get_required_osd_release(unit, model)
    # The actual release which OSDs are on
    current_running_osd_release = await _get_current_osd_release(unit, model)

    if current_require_osd_release != current_running_osd_release:
        set_command = f"ceph osd require-osd-release {current_running_osd_release}"
        await model.run_on_unit(unit_name=unit, command=set_command, timeout=600)


# Private functions
async def _get_required_osd_release(unit: str, model: Model) -> str:
    """Get the value of require-osd-release option on a ceph-mon unit.

    :param unit: The ceph-mon unit name where the check command runs on.
    :type unit: str
    :param model: Model object
    :type model: Model
    :return: the value of require-osd-release option
    :rtype: str
    :raises CommandRunFailed: When a command fails to run.
    """
    check_command = "ceph osd dump -f json"

    check_option_result = await model.run_on_unit(
        unit_name=unit, command=check_command, timeout=600
    )
    current_require_osd_release = json.loads(check_option_result["Stdout"]).get(
        "require_osd_release", ""
    )
    logger.debug("Current require-osd-release is set to: %s", current_require_osd_release)

    return current_require_osd_release


async def _get_current_osd_release(unit: str, model: Model) -> str:
    """Get the current release of OSDs.

    The release of OSDs is parsed from the output of running `ceph versions` command
    on a ceph-mon unit.
    :param unit: The ceph-mon unit name where the check command runs on.
    :type unit: str
    :param model: Model object
    :type model: Model
    :return: the release which OSDs are on
    :rtype: str
    :raises RunUpgradeError: When an upgrade fails.
    :raises CommandRunFailed: When a command fails to run.
    """
    check_command = "ceph versions -f json"

    check_osd_result = await model.run_on_unit(unit_name=unit, command=check_command, timeout=600)

    osd_release_output = json.loads(check_osd_result["Stdout"]).get("osd", None)
    # throw exception if ceph-mon doesn't contain osd release information in `ceph`
    if not osd_release_output:
        raise RunUpgradeError(f"Cannot get OSD release information on ceph-mon unit '{unit}'.")
    # throw exception if OSDs are on mismatched releases
    if len(osd_release_output) > 1:
        raise RunUpgradeError(
            f"OSDs are on mismatched releases:\n{osd_release_output}."
            "Please manually upgrade them to be on the same release before proceeding."
        )

    # get release name from "osd_release_output". Example value of "osd_release_output":
    # {'ceph version 15.2.17 (8a82819d84cf884bd39c17e3236e0632) octopus (stable)': 1}
    osd_release_key, *_ = osd_release_output.keys()
    current_osd_release = osd_release_key.split(" ")[4].strip()
    ceph_releases = ", ".join(CEPH_RELEASES)
    if current_osd_release not in ceph_releases:
        raise RunUpgradeError(
            f"Cannot recognize Ceph release '{current_osd_release}'. The supporting "
            f"releases are: {ceph_releases}"
        )
    logger.debug("Currently OSDs are on the '%s' release", current_osd_release)

    return current_osd_release


def stringify_units(units: Iterable[Unit]) -> str:
    """Convert Units into a comma-separated string of unit names, sorted alphabetically.

    :param units: An iterable of Unit objects to be converted.
    :type units: Iterable[Unit]
    :return: A comma-separated string of sorted unit names.
    :rtype: str
    """
    sorted_unit_names = sorted([unit.name for unit in units])
    return ", ".join(sorted_unit_names)
