"""Logic for generating distributions from a :ref:`Project`."""

import io
import os
import posixpath
import subprocess
import tarfile
import textwrap
from pathlib import Path
from typing import Callable, Optional, Tuple

from .errors import DiagnosticError
from .nodejs import generate_assets
from .project import Project
from .wheelfile import WheelFile


def _get_vcs_tracked_files(path: Path) -> Optional[Tuple[str, ...]]:
    if not (path / ".git").is_dir():
        return None

    outb = subprocess.check_output(
        ["git", "ls-files", "--recurse-submodules", "-z"],
        cwd=path,
    )
    return tuple(
        os.fsdecode(location) for location in outb.strip(b"\0").split(b"\0") if location
    )


def _sdist_filter(
    project: Project,
) -> Callable[[tarfile.TarInfo], Optional[tarfile.TarInfo]]:
    """Create a filter to pass to tarfile.add, for this project."""
    compiled_assets = project.compiled_assets
    tracked_files = _get_vcs_tracked_files(project.location)

    def _filter(tarinfo: tarfile.TarInfo) -> Optional[tarfile.TarInfo]:
        name = tarinfo.name
        # Exclude the entry for the root.
        if not name:
            return None

        # Exclude compiled pyc files.
        if posixpath.basename(name) == "__pycache__":
            return None

        # Exclude compiled assets.
        if name in compiled_assets:
            return None

        # Exclude things that are excluded from version control.
        if tracked_files is not None and name not in tracked_files:
            return None

        return tarinfo

    # NOTE: The CLI for build needs to check that the compiled assets we have here, are
    #       not tracked under version control.
    return _filter


#
# External API
#
def generate_source_distribution(
    project: Project,
    *,
    destination: Path,
) -> str:
    """Generate a source distribution for project, and place it inside destination.

    :return: Name of the generated source tarball.
    """
    os.makedirs(destination, exist_ok=True)

    dashed_pair = f"{project.snake_name}-{project.version}"
    sdist_tarball = destination / f"{dashed_pair}.tar.gz"

    # NOTE: Post Python 3.7 -- can drop the format kwarg, since it'll be the default.
    with tarfile.open(
        name=sdist_tarball, mode="w:gz", format=tarfile.PAX_FORMAT
    ) as tarball:
        # Recursively add the files to this tarball, with an exclusion filter.
        tarball.add(
            project.location,
            arcname="",
            recursive=True,
            filter=_sdist_filter(project),
        )

        # Write the metadata file.
        metadata_content = project.get_metadata_file_contents().encode()
        tarinfo = tarfile.TarInfo(posixpath.join(dashed_pair, "PKG-INFO"))
        tarinfo.size = len(metadata_content)
        tarball.addfile(tarinfo, io.BytesIO(metadata_content))

    return sdist_tarball.name


def generate_metadata(
    project: Project,
    *,
    destination: Path,
) -> str:
    """Generate the metadata (.dist-info) and place it in `metadata_directory`.

    :return: name of the dist-info directory generated.
    """
    dist_info = destination / f"{project.snake_name}-{project.version}.dist-info"
    try:
        os.makedirs(dist_info)
    except OSError as error:
        raise DiagnosticError(
            message="Metadata directory already exists",
            context=None,
            hint_stmt=None,
        ) from error

    # Delegated generation.
    (dist_info / "entry_points.txt").write_text(project.get_entry_points_contents())
    (dist_info / "LICENSE").write_text(project.get_license_contents())
    (dist_info / "METADATA").write_text(project.get_metadata_file_contents())

    # Templated generation.
    (dist_info / "WHEEL").write_text(
        textwrap.dedent(
            """\
            Wheel-Version: 1.0
            Generator: flit {version}
            Root-Is-Purelib: true
            Tag: py3-none-any
            """
        )
    )

    return dist_info.name


def generate_wheel_distribution(
    project: Project,
    *,
    destination: Path,
    metadata_directory: Path,
    editable: bool,
) -> str:
    # Generate the JS / CSS assets
    generate_assets(project)

    wheel_path = (
        destination / f"{project.snake_name}-{project.version}-py3-none-any.whl"
    )
    with WheelFile(
        path=wheel_path,
        tracked_files=_get_vcs_tracked_files(project.location),
        compiled_assets=project.compiled_assets,
    ) as wheel:
        if editable:
            # Generate a .pth file, for editable installs. There's an enforced src/
            # directory, so this is fairly safe to do.
            wheel.add_string(
                os.fsdecode(project.source_path.resolve()),
                dest=project.snake_name + ".pth",
            )
        else:
            # Add files from the project's src/ directory.
            wheel.add_directory(project.source_path, dest="")

        # Put the metadata at the end.
        # https://www.python.org/dev/peps/pep-0427/#recommended-archiver-features
        wheel.add_directory(metadata_directory, dest=metadata_directory.name)
        wheel.write_record(dest=metadata_directory.name + "/RECORD")

    return wheel.name
