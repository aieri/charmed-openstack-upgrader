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

"""Application class."""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from io import StringIO
from typing import Any, Optional

from juju.client._definitions import ApplicationStatus
from ruamel.yaml import YAML

from cou.exceptions import MismatchedOpenStackVersions
from cou.utils.juju_utils import extract_charm_name_from_url
from cou.utils.openstack import OpenStackCodenameLookup, OpenStackRelease

logger = logging.getLogger(__name__)


@dataclass
class Application:
    """Representation of an application in the deployment.

    :param name: name of the application
    :type name: str
    :param status: Status of the application.
    :type status: ApplicationStatus
    :param config: Configuration of the application.
    :type config: dict
    :param model_name: Model name.
    :type model_name: str
    :param charm: Name of the charm.
    :type charm: str
    :param charm_origin: Origin of the charm (local, ch, cs and etc.), defaults to ""
    :type charm_origin: str, defaults to ""
    :param os_origin: OpenStack origin of the application. E.g: cloud:focal-wallaby, defaults to ""
    :type os_origin: str, defaults to ""
    :param channel: Channel that the charm tracks. E.g: "ussuri/stable", defaults to ""
    :type channel: str, defaults to ""
    :param units: Units representation of an application.
        E.g: {"keystone/0": {'os_version': 'victoria', 'workload_version': '2:18.1'}}
    :type units: defaultdict[str, dict]
    """

    # pylint: disable=too-many-instance-attributes

    name: str
    status: ApplicationStatus
    config: dict
    model_name: str
    charm: str = ""
    charm_origin: str = ""
    os_origin: str = ""
    channel: str = ""
    units: defaultdict[str, dict] = field(default_factory=lambda: defaultdict(dict))

    def __post_init__(self) -> None:
        """Initialize the Application dataclass."""
        self.charm = extract_charm_name_from_url(self.status.charm)
        self.channel = self.status.charm_channel
        self.charm_origin = self.status.charm.split(":")[0]
        self.os_origin = self._get_os_origin()
        for unit in self.status.units.keys():
            workload_version = self.status.units[unit].workload_version
            self.units[unit]["workload_version"] = workload_version
            compatible_os_versions = OpenStackCodenameLookup.lookup(self.charm, workload_version)
            # NOTE(gabrielcocenza) get the latest compatible OpenStack version.
            if compatible_os_versions:
                unit_os_version = max(compatible_os_versions)
                self.units[unit]["os_version"] = unit_os_version
            else:
                self.units[unit]["os_version"] = None
                logger.warning(
                    "No compatible OpenStack versions were found to %s with workload version %s",
                    self.name,
                    workload_version,
                )

    def __hash__(self) -> int:
        """Hash magic method for Application.

        :return: Unique hash identifier for Application object.
        :rtype: int
        """
        return hash(f"{self.name}{self.charm}")

    def __eq__(self, other: Any) -> bool:
        """Equal magic method for Application.

        :param other: Application object to compare.
        :type other: Any
        :return: True if equal False if different.
        :rtype: bool
        """
        return other.name == self.name and other.charm == self.charm

    def __str__(self) -> str:
        """Dump as string.

        :return: Summary representation of an Application.
        :rtype: str
        """
        summary = {
            self.name: {
                "model_name": self.model_name,
                "charm": self.charm,
                "charm_origin": self.charm_origin,
                "os_origin": self.os_origin,
                "channel": self.channel,
                "units": {
                    unit: {
                        "workload_version": details.get("workload_version", ""),
                        "os_version": str(details.get("os_version")),
                    }
                    for unit, details in self.units.items()
                },
            }
        }
        yaml = YAML()
        with StringIO() as stream:
            yaml.dump(summary, stream)
            return stream.getvalue()

    @property
    def series(self) -> str:
        """Ubuntu series of the application.

        :return: Ubuntu series of application. E.g: focal
        :rtype: str
        """
        return self.status.series

    @property
    def current_os_release(self) -> Optional[OpenStackRelease]:
        """Current OpenStack Release of the application.

        :raises MismatchedOpenStackVersions: Raise MismatchedOpenStackVersions if units of
            an application are running mismatched OpenStack versions.
        :return: OpenStackRelease object
        :rtype: OpenStackRelease
        """
        os_versions = {unit_values.get("os_version") for unit_values in self.units.values()}
        if not os_versions:
            # TODO(gabrielcocenza) subordinate charms doesn't have units on ApplicationStatus and
            # return an empty set. This should be handled by a future implementation of
            # subordinate applications class.
            return None
        if len(os_versions) == 1:
            return os_versions.pop()
        # NOTE (gabrielcocenza) on applications that use single-unit or paused-single-unit
        # upgrade methods, more than one version can be found.
        logger.error(
            (
                "Units of application %s are running mismatched OpenStack versions: %s. "
                "This is not currently handled."
            ),
            self.name,
            os_versions,
        )
        raise MismatchedOpenStackVersions()

    def _get_os_origin(self) -> str:
        """Get application configuration for openstack-origin or source.

        :return: Configuration parameter of the charm to set OpenStack origin.
            E.g: cloud:focal-wallaby
        :rtype: str
        """
        for origin in ("openstack-origin", "source"):
            if self.config.get(origin):
                return self.config[origin].get("value", "")

        logger.warning("Failed to get origin for %s, no origin config found", self.name)
        return ""