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
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from cou.apps.base import OpenStackApplication
from cou.exceptions import (
    HaltUpgradePlanGeneration,
    HighestReleaseAchieved,
    NoTargetError,
    OutOfSupportRange,
)
from cou.steps import (
    ApplicationUpgradePlan,
    PostUpgradeStep,
    PreUpgradeStep,
    UnitUpgradeStep,
    UpgradePlan,
    UpgradeStep,
)
from cou.steps.analyze import Analysis
from cou.steps.backup import backup
from cou.steps.plan import (
    create_upgrade_group,
    determine_upgrade_target,
    generate_plan,
    manually_upgrade_data_plane,
)
from cou.utils import app_utils
from cou.utils.openstack import OpenStackRelease


def generate_expected_upgrade_plan_principal(app, target, model):
    expected_plan = ApplicationUpgradePlan(
        description=f"Upgrade plan for '{app.name}' to {target.codename}"
    )
    if app.charm in ["rabbitmq-server", "ceph-mon", "keystone"]:
        # apps waiting for whole model
        wait_step = PostUpgradeStep(
            description=f"Wait 1800s for model {model.name} to reach the idle state.",
            parallel=False,
            coro=model.wait_for_active_idle(1800, apps=None),
        )
    else:
        wait_step = PostUpgradeStep(
            description=f"Wait 300s for app {app.name} to reach the idle state.",
            parallel=False,
            coro=model.wait_for_active_idle(300, apps=[app.name]),
        )

    upgrade_packages = PreUpgradeStep(
        description=f"Upgrade software packages of '{app.name}' from the current APT repositories",
        parallel=True,
    )
    for unit in app.units:
        upgrade_packages.add_step(
            UnitUpgradeStep(
                description=f"Upgrade software packages on unit {unit.name}",
                coro=app_utils.upgrade_packages(unit.name, model, None),
            )
        )

    upgrade_steps = [
        upgrade_packages,
        PreUpgradeStep(
            description=(
                f"Refresh '{app.name}' to the latest revision of "
                f"'{target.previous_release}/stable'"
            ),
            parallel=False,
            coro=model.upgrade_charm(app.name, f"{target.previous_release}/stable", switch=None),
        ),
        UpgradeStep(
            description=f"Change charm config of '{app.name}' 'action-managed-upgrade' to False.",
            parallel=False,
            coro=model.set_application_config(app.name, {"action-managed-upgrade": False}),
        ),
        UpgradeStep(
            description=(f"Upgrade '{app.name}' to the new channel: '{target.codename}/stable'"),
            parallel=False,
            coro=model.upgrade_charm(app.name, f"{target.codename}/stable"),
        ),
        UpgradeStep(
            description=(
                f"Change charm config of '{app.name}' "
                f"'{app.origin_setting}' to 'cloud:focal-{target.codename}'"
            ),
            parallel=False,
            coro=model.set_application_config(
                app.name, {f"{app.origin_setting}": f"cloud:focal-{target.codename}"}
            ),
        ),
        wait_step,
        PostUpgradeStep(
            description=f"Check if the workload of '{app.name}' has been upgraded",
            parallel=False,
            coro=app._check_upgrade(target),
        ),
    ]
    expected_plan.add_steps(upgrade_steps)
    return expected_plan


def generate_expected_upgrade_plan_subordinate(app, target, model):
    expected_plan = ApplicationUpgradePlan(
        description=f"Upgrade plan for '{app.name}' to {target}"
    )
    upgrade_steps = [
        PreUpgradeStep(
            description=(
                f"Refresh '{app.name}' to the latest revision of "
                f"'{target.previous_release}/stable'"
            ),
            parallel=False,
            coro=model.upgrade_charm(app.name, f"{target.previous_release}/stable", switch=None),
        ),
        UpgradeStep(
            description=(f"Upgrade '{app.name}' to the new channel: '{target.codename}/stable'"),
            parallel=False,
            coro=model.upgrade_charm(app.name, f"{target.codename}/stable"),
        ),
    ]
    expected_plan.add_steps(upgrade_steps)
    return expected_plan


@pytest.mark.asyncio
async def test_generate_plan(apps, model, cli_args):
    target = OpenStackRelease("victoria")
    app_keystone = apps["keystone_focal_ussuri"]
    app_cinder = apps["cinder_focal_ussuri"]
    app_keystone_ldap = apps["keystone_ldap_focal_ussuri"]
    analysis_result = Analysis(
        model=model,
        apps_control_plane=[app_keystone, app_cinder, app_keystone_ldap],
        apps_data_plane=[],
    )

    upgrade_plan = await generate_plan(analysis_result, cli_args)

    expected_plan = UpgradePlan("Upgrade cloud from 'ussuri' to 'victoria'")
    expected_plan.add_step(
        PreUpgradeStep(
            description="Verify that all OpenStack applications are in idle state",
            parallel=False,
            coro=analysis_result.model.wait_for_active_idle(
                timeout=11, idle_period=10, raise_on_blocked=True
            ),
        )
    )
    expected_plan.add_step(
        PreUpgradeStep(
            description="Backup mysql databases",
            parallel=False,
            coro=backup(model),
        )
    )

    control_plane_principals = UpgradePlan("Control Plane principal(s) upgrade plan")
    keystone_plan = generate_expected_upgrade_plan_principal(app_keystone, target, model)
    cinder_plan = generate_expected_upgrade_plan_principal(app_cinder, target, model)
    control_plane_principals.add_step(keystone_plan)
    control_plane_principals.add_step(cinder_plan)

    control_plane_subordinates = UpgradePlan("Control Plane subordinate(s) upgrade plan")
    keystone_ldap_plan = generate_expected_upgrade_plan_subordinate(
        app_keystone_ldap, target, model
    )
    control_plane_subordinates.add_step(keystone_ldap_plan)

    expected_plan.add_step(control_plane_principals)
    expected_plan.add_step(control_plane_subordinates)
    assert upgrade_plan == expected_plan


@pytest.mark.parametrize(
    "current_os_release, current_series, next_release",
    [
        (OpenStackRelease("victoria"), "focal", "wallaby"),
        (OpenStackRelease("xena"), "focal", "yoga"),
    ],
)
def test_determine_upgrade_target(current_os_release, current_series, next_release):
    target = determine_upgrade_target(current_os_release, current_series)

    assert target == next_release


def test_determine_upgrade_target_no_upgrade_available():
    current_os_release = OpenStackRelease("yoga")
    current_series = "focal"
    with pytest.raises(HighestReleaseAchieved):
        determine_upgrade_target(current_os_release, current_series)


@pytest.mark.parametrize(
    "current_os_release, current_series, exp_error_msg",
    [
        (
            None,
            "bionic",
            "Cannot determine the current OS release in the cloud. "
            "Is this a valid OpenStack cloud?",
        ),  # current_os_release is None
        (
            OpenStackRelease("ussuri"),
            None,
            "Cannot determine the current Ubuntu series in the cloud. "
            "Is this a valid OpenStack cloud?",
        ),  # current_series is None
    ],
)
def test_determine_upgrade_target_invalid_input(current_os_release, current_series, exp_error_msg):
    with pytest.raises(NoTargetError, match=exp_error_msg):
        determine_upgrade_target(current_os_release, current_series)


def test_determine_upgrade_target_no_next_release():
    exp_error_msg = "Cannot find target to upgrade. Current minimum OS release is "
    "'ussuri'. Current Ubuntu series is 'focal'."
    current_series = "focal"

    with pytest.raises(NoTargetError, match=exp_error_msg), patch(
        "cou.utils.openstack.OpenStackRelease.next_release", new_callable=PropertyMock
    ) as mock_next_release:
        mock_next_release.return_value = None
        current_os_release = OpenStackRelease(
            "ussuri"
        )  # instantiate OpenStackRelease with any valid codename
        determine_upgrade_target(current_os_release, current_series)


@pytest.mark.parametrize(
    "current_os_release, current_series",
    [
        (OpenStackRelease("yoga"), "jammy"),
        (OpenStackRelease("train"), "bionic"),
        (OpenStackRelease("zed"), "focal"),
    ],
)
def test_determine_upgrade_target_release_out_of_range(current_os_release, current_series):
    with pytest.raises(OutOfSupportRange):
        determine_upgrade_target(current_os_release, current_series)


@pytest.mark.asyncio
async def test_create_upgrade_plan():
    """Test create_upgrade_group."""
    app: OpenStackApplication = MagicMock(spec_set=OpenStackApplication)
    app.generate_upgrade_plan.return_value = MagicMock(spec_set=ApplicationUpgradePlan)
    target = OpenStackRelease("victoria")
    description = "test"

    plan = await create_upgrade_group([app], target, description, lambda *_: True)

    assert plan.description == description
    assert plan.parallel is False
    assert plan._coro is None
    assert len(plan.sub_steps) == 1
    assert plan.sub_steps[0] == app.generate_upgrade_plan.return_value
    app.generate_upgrade_plan.assert_called_once_with(target)


@pytest.mark.asyncio
async def test_create_upgrade_plan_HaltUpgradePlanGeneration():
    """Test create_upgrade_group."""
    app: OpenStackApplication = MagicMock(spec=OpenStackApplication)
    app.name = "test-app"
    app.generate_upgrade_plan.side_effect = HaltUpgradePlanGeneration
    target = OpenStackRelease("victoria")
    description = "test"

    plan = await create_upgrade_group([app], target, description, lambda *_: True)

    assert len(plan.sub_steps) == 0
    app.generate_upgrade_plan.assert_called_once_with(target)


@pytest.mark.asyncio
async def test_create_upgrade_plan_failed():
    """Test create_upgrade_group."""
    app: OpenStackApplication = MagicMock(spec=OpenStackApplication)
    app.name = "test-app"
    app.generate_upgrade_plan.side_effect = Exception("test")

    with pytest.raises(Exception, match="test"):
        await create_upgrade_group([app], "victoria", "test", lambda *_: True)


@patch("builtins.print")
def test_plan_print_warn_manually_upgrade(mock_print, model, apps):
    result = Analysis(
        model=model,
        apps_control_plane=[apps["keystone_focal_wallaby"]],
        apps_data_plane=[apps["nova_focal_ussuri"]],
    )
    manually_upgrade_data_plane(result)
    mock_print.assert_called_with(
        "WARNING: Please upgrade manually the data plane apps: nova-compute"
    )


@patch("builtins.print")
def test_analysis_not_print_warn_manually_upgrade(mock_print, model, apps):
    result = Analysis(
        model=model,
        apps_control_plane=[apps["keystone_focal_ussuri"]],
        apps_data_plane=[apps["nova_focal_ussuri"]],
    )
    manually_upgrade_data_plane(result)
    mock_print.assert_not_called()