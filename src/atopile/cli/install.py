# pylint: disable=logging-fstring-interpolation

"""
This CLI command provides the `ato install` command to:
- install dependencies
- download JLCPCB footprints
"""

import logging
import subprocess
import sys
from pathlib import Path
from typing import Annotated, Optional
from urllib.parse import urlparse

import requests
import ruamel.yaml
import typer
from git import GitCommandError, InvalidGitRepositoryError, NoSuchPathError, Repo

import atopile.config
import faebryk.libs.exceptions
from atopile import errors, version
from atopile.utils import robustly_rm_dir

yaml = ruamel.yaml.YAML()

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)


def install(
    to_install: Annotated[str | None, typer.Argument()] = None,
    jlcpcb: Annotated[
        bool, typer.Option("--jlcpcb", "-j", help="JLCPCB component ID")
    ] = False,
    link: Annotated[
        bool,
        typer.Option("--link", "-l", help="Keep this dependency linked to the source"),
    ] = False,
    upgrade: Annotated[
        bool, typer.Option("--upgrade", "-u", help="Upgrade dependencies")
    ] = False,
    path: Annotated[Path | None, typer.Argument()] = None,
):
    """
    Install atopile packages or components from jlcpcb.com/parts
    """
    do_install(to_install, jlcpcb, link, upgrade, path)


def do_install(
    to_install: str | None, jlcpcb: bool, link: bool, upgrade: bool, path: Path | None
):
    """
    Actually do the installation of the dependencies.
    This is split in two so that it can be called from `install` and `create`
    """

    current_path = Path.cwd()
    config = atopile.config.get_project_config_from_path(Path(path or current_path))
    ctx = atopile.config.ProjectContext.from_config(config)
    top_level_path = config.location

    log.info(f"Installing {to_install + ' ' if to_install else ''}in {top_level_path}")

    if jlcpcb:
        if to_install is None:
            raise errors.UserBadParameterError("No component ID specified")
        # eg. "ato install --jlcpcb=C123"
        install_jlcpcb(to_install, top_level_path)
    elif to_install:
        # eg. "ato install some-atopile-module"
        install_single_dependency(to_install, link, upgrade, config, ctx)
    else:
        # eg. "ato install"
        install_project_dependencies(config, ctx, upgrade)

    log.info("[green]Done![/] :call_me_hand:", extra={"markup": True})


def get_package_repo_from_registry(module_name: str) -> str:
    """
    Get the git repo for a package from the ato registry.
    """
    try:
        response = requests.post(
            "https://get-package-atsuhzfd5a-uc.a.run.app",
            json={"name": module_name},
            timeout=10,
        )
    except requests.exceptions.ReadTimeout as ex:
        raise errors.UserInfraError(
            f"Request to registry timed out for package '{module_name}'"
        ) from ex

    if response.status_code == 500:
        raise errors.UserException(
            f"Could not find package '{module_name}' in registry."
        )
    response.raise_for_status()
    return_data = response.json()
    try:
        return_url = return_data["data"]["repo_url"]
    except KeyError as ex:
        raise errors.UserException(
            f"No repo_url found for package '{module_name}'"
        ) from ex
    return return_url


def install_single_dependency(
    to_install: str,
    link: bool,
    upgrade: bool,
    config: atopile.config.ProjectConfig,
    ctx: atopile.config.ProjectContext,
):
    dependency = atopile.config.Dependency.from_str(to_install)
    name = _name_and_clone_url_helper(dependency.name)[0]
    if link:
        dependency.link_broken = False
        abs_path = ctx.module_path / name
        dependency.path = abs_path.relative_to(ctx.project_path)
    else:
        abs_path = ctx.src_path / name
        dependency.path = abs_path.relative_to(ctx.project_path)
        dependency.link_broken = True

    try:
        installed_version = install_dependency(dependency, upgrade, abs_path)
    except GitCommandError as ex:
        if "already exists and is not an empty directory" in ex.stderr:
            # FIXME: shouldn't `--upgrade` do this already?
            raise errors.UserException(
                f"Directory {abs_path} already exists and is not empty. "
                "Please move or remove it before installing this new content."
            ) from ex
        raise
    # If the link's broken, remove the .git directory so git treats it as copy-pasted code
    if dependency.link_broken:
        try:
            robustly_rm_dir(abs_path / ".git")
        except (PermissionError, OSError, FileNotFoundError) as ex:
            errors.UserException(f"Failed to remove .git directory: {repr(ex)}").log(
                log, logging.WARNING
            )

    if dependency.version_spec is None and installed_version:
        # If the user didn't specify a version, we'll
        # use the one we just installed as a basis
        dependency.version_spec = f"@{installed_version}"

    names = {dep.name: i for i, dep in enumerate(config.dependencies)}
    if dependency.name in names:
        config.dependencies[names[dependency.name]] = dependency
    else:
        config.dependencies.append(dependency)
    config.save_changes()


def install_project_dependencies(
    config: atopile.config.ProjectConfig,
    ctx: atopile.config.ProjectContext,
    upgrade: bool,
):
    for _ctx, dependency in faebryk.libs.exceptions.iter_through_errors(
        config.dependencies
    ):
        with _ctx():
            if not dependency.link_broken:
                # FIXME: these dependency objects are a little too entangled
                name = _name_and_clone_url_helper(dependency.name)[0]
                abs_path = ctx.module_path / name
                dependency.path = abs_path.relative_to(ctx.project_path)

                try:
                    install_dependency(dependency, upgrade, abs_path)
                except GitCommandError as ex:
                    if "already exists and is not an empty directory" in ex.stderr:
                        # FIXME: shouldn't `--upgrade` do this already?
                        raise errors.UserException(
                            f"Directory {abs_path} already exists and is not empty. "
                            "Please move or remove it before installing this new content."
                        ) from ex
                    raise


def install_dependency(
    dependency: atopile.config.Dependency, upgrade: bool, abs_path: Path
) -> Optional[str]:
    """
    Install a dependency of the name "module_name"
    """
    # Ensure the modules path exists
    abs_path.parent.mkdir(parents=True, exist_ok=True)

    # Figure out what we're trying to install here
    module_spec = dependency.version_spec or "*"
    module_name, clone_url = _name_and_clone_url_helper(dependency.name)

    try:
        # This will raise an exception if the directory does not exist
        repo = Repo(abs_path)
    except (InvalidGitRepositoryError, NoSuchPathError):
        # Directory does not contain a valid repo, clone into it
        log.info(f"Installing dependency {module_name}")
        repo = Repo.clone_from(clone_url, abs_path)
        repo.active_branch.tracking_branch().checkout()
    else:
        # In this case the directory exists and contains a valid repo
        if upgrade:
            log.info(f"Fetching latest changes for {module_name}")
            repo.remotes.origin.fetch()
        else:
            log.info(
                f"{module_name} already exists. If you wish to upgrade, use --upgrade"
            )
            # here we're done because we don't want to play with peoples' deps under them
            return

    # Figure out what version of this thing we need
    semver_to_tag = {}
    installed_semver = None
    for tag in repo.tags:
        try:
            semver_to_tag[version.parse(tag.name)] = tag
        except errors.UserException:
            log.debug(f"Tag {tag.name} is not a valid semver tag. Skipping.")

    if "@" in module_spec:
        # If there's an @ in the version, we're gonna check that thing out
        best_checkout = module_spec.strip(" @")
    elif semver_to_tag:
        # Otherwise we're gonna find the best tag meeting the semver spec
        valid_versions = [v for v in semver_to_tag if version.match(module_spec, v)]
        if not valid_versions:
            raise errors.UserException(
                f"No versions of {module_name} match spec {module_spec}.\n"
                f"Available versions: {', '.join(map(str, semver_to_tag))}"
            )
        installed_semver = max(valid_versions)
        best_checkout = semver_to_tag[installed_semver]
    else:
        log.warning(
            "No semver tags found for this module. Using latest default branch :hot_pepper:.",
            extra={"markup": True},
        )
        return None

    # If the repo is dirty, throw an error
    if repo.is_dirty():
        raise errors.UserException(
            f"Module {module_name} has uncommitted changes. Aborting."
        )

    # Checkout the best thing we've found
    ref_before_checkout = repo.head.commit

    # If the repo best_checkout is a branch, we need to checkout the origin/branch
    if best_checkout in repo.heads:
        best_checkout = f"origin/{best_checkout}"

    repo.git.checkout(best_checkout)

    if repo.head.commit == ref_before_checkout:
        log.info(
            f"Already on the best option ([cyan bold]{best_checkout}[/]) for {module_name}",
            extra={"markup": True},
        )
    else:
        log.info(
            f"Using :sparkles: [cyan bold]{best_checkout}[/] :sparkles: of {module_name}",
            extra={"markup": True},
        )

    return repo.head.commit.hexsha


def install_jlcpcb(component_id: str, top_level_path: Path):
    """Install a component from JLCPCB"""
    component_id = component_id.upper()
    if not component_id.startswith("C") or not component_id[1:].isdigit():
        raise errors.UserException(f"Component id {component_id} is invalid. Aborting.")

    footprints_dir = (
        top_level_path
        / atopile.config.get_project_config_from_path(top_level_path).paths.footprints
    )
    footprints_dir.mkdir(parents=True, exist_ok=True)

    ato_src_dir = (
        top_level_path
        / atopile.config.get_project_config_from_path(top_level_path).paths.src
    )
    ato_src_dir.mkdir(parents=True, exist_ok=True)

    log.info(f"Footprints directory: {footprints_dir}")

    command = [
        sys.executable,
        "-m",
        "easyeda2kicad",
        "--full",
        f"--lcsc_id={component_id}",
        f"--output={footprints_dir}",
        "--overwrite",
        "--ato",
        f"--ato_file_path={ato_src_dir}",
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)

    # The stdout and stderr are captured due to 'capture_output=True'
    print("STDOUT:", result.stdout)
    print("STDERR:", result.stderr)

    # Check the return code to see if the command was successful
    if result.returncode == 0:
        print("Command executed successfully")
    else:
        component_link = f"https://jlcpcb.com/partdetail/{component_id}"
        raise errors.UserException(
            "Oh no! Looks like this component doesnt have a model available. "
            f"More information about the component can be found here: {component_link}"
        )


def _name_and_clone_url_helper(name: str) -> tuple[str, str]:
    """Return the name of the package and the URL to clone it from."""
    parsed_url = urlparse(name)
    if not parsed_url.scheme:
        return name, get_package_repo_from_registry(name)
    else:
        splitted = parsed_url.path.split("/")
        return splitted[-1] or splitted[-2], name
