import json
import logging
import typing as t
from enum import Enum
from pathlib import Path

import requests
import semver

from mlcube.errors import ExecutionError
from mlcube.shell import Shell
from mlcube.system_settings import SystemSettings

__all__ = ["Runtime", "Version", "ImageSpec", "Client", "DockerHubClient"]

logger = logging.getLogger(__name__)


class Runtime(Enum):
    """Container runtime"""

    UNKNOWN = 0
    APPTAINER = 1
    SINGULARITY = 2
    """Singularity / SingularityCE
    
    SingularityCE
        https://github.com/sylabs/singularity/releases/tag/v3.8.0
        This is the first release of SingularityCE 3.8.0, the Community Edition of the Singularity container runtime.
        The package name for this release is now `singularity-ce`.
    
    Singularity
        https://github.com/sylabs/singularity/releases/tag/v3.7.4
        Singularity 3.7.4 is the most recent stable release of Singularity prior to Sylabs' fork from 
        `github.com/hpcng/singularity`.
    """


class Version:
    def __init__(self, runtime: Runtime, version: semver.VersionInfo) -> None:
        self.runtime = runtime
        self.version = version

    def __str__(self) -> str:
        return f"Version(runtime={self.runtime.name}, version={self.version})"

    @classmethod
    def from_version_string(cls, version_string: str) -> "Version":
        version_string = version_string.strip()
        if version_string.startswith("singularity version "):
            runtime, version_string = (
                Runtime.SINGULARITY,
                version_string[20:].strip(),
            )
        elif version_string.startswith("singularity-ce version "):
            runtime, version_string = (
                Runtime.SINGULARITY,
                version_string[23:].strip(),
            )
        elif version_string.startswith("apptainer version "):
            runtime, version_string = Runtime.APPTAINER, version_string[18:].strip()
        elif "/" in version_string:  # Handle old stuff like "x.y.z-pull/123-0a5d"
            runtime, version_string = Runtime.SINGULARITY, version_string.replace(
                "/", "+", 1
            )
        else:
            logger.warning(
                "Version.from_version_string unrecognized container runtime (version_string: %s)",
                version_string,
            )
            runtime = Runtime.UNKNOWN
        return Version(runtime, semver.VersionInfo.parse(version_string))


class ImageSpec(Enum):
    """Build specification format for building singularity images.

    Primary purpose of this enum is to help MLCube guess how to compute the hash for MLCube-based project.
    """

    OTHER = 0
    """Other type pretty much means everything that's not covered by types defined below."""

    DOCKER = 1
    """Docker image ('docker://')."""

    DOCKER_ARCHIVE = 2
    """Local tar files ('docker-archive:')."""

    SINGULARITY = 3
    """Singularity Image File."""


class Client:
    """Singularity container platform client.

    Args:
        singularity: A shell command to run singularity. When it's string, it needs to be constructed so that splitting
        using space character resulted in correct sequence of commands and arguments.
    """

    @classmethod
    def from_env(cls) -> "Client":
        # Check if system settings file contains information about singularity.
        executables = ["singularity", "sudo singularity", "apptainer", "sudo apptainer"]

        system_settings = SystemSettings()
        if "singularity" in system_settings.platforms:
            executables = [
                system_settings.platforms["singularity"]["singularity"]
            ] + executables
            logger.info(
                "Client.from_env found singularity platform config in MLCube system settings file "
                "(file=%s, platform=%s). Will try it first for detecting singularity CLI client.",
                system_settings.path.as_posix(),
                system_settings.platforms["singularity"],
            )
        logger.debug(
            "Client.from_env will try candidate executables in the following order "
            "(first available will be selected): %s",
            executables,
        )

        for executable in executables:
            try:
                client = Client(executable)
                logger.info(
                    "Client.from_env found singularity (exec=%s, version=%s)",
                    client.singularity,
                    client.version,
                )
                return client
            except ExecutionError:
                logger.warning(
                    "Client.from_env failed to run singularity as: %s", executable
                )
                continue

        raise ExecutionError(
            f"Failed to identify proper singularity client. I tried the following: {executables}."
        )

    def supports_fakeroot(self) -> bool:
        singularity_35 = (
            self.version.runtime == Runtime.SINGULARITY
            and self.version.version >= semver.VersionInfo(major=3, minor=5)
        )
        apptainer = self.version.runtime == Runtime.APPTAINER
        return singularity_35 or apptainer

    def __init__(
        self, singularity: t.Union[str, t.List], version: t.Optional[Version] = None
    ) -> None:
        if isinstance(singularity, str):
            singularity = singularity.split(" ")
        self.singularity: t.List[str] = [c.strip() for c in singularity if c.strip()]
        self.version: t.Optional[Version] = version
        self.init()
        logger.debug(
            "Client.__init__ executable=%s, version=%s", self.singularity, self.version
        )

    def init(self, force: bool = False) -> None:
        if force:
            self.version = None
        if self.version is None:
            version_cmd = self.singularity + ["--version"]
            exit_code, version_string = Shell.run_and_capture_output(version_cmd)
            if exit_code != 0:
                raise ExecutionError(
                    f"Singularity client failed to initialize. The following command ({version_cmd}) returned non-zero"
                    f"exit code ({exit_code}). MLCube cannot run singularity images unless this check passes.",
                    function=f"{self.__class__}.init",
                    args={
                        "force": force,
                        "singularity": self.singularity,
                        "version_cmd": version_cmd,
                    },
                )
            self.version = Version.from_version_string(version_string)
            logger.debug("Client.init version=%s", self.version)

    def build(
        self,
        build_dir: str,
        recipe: str,
        image_dir: str,
        image_name: str,
        build_args: str,
    ) -> None:
        # Get full path to a singularity image. By design, we compute it relative to {mlcube.root}/workspace.
        image_file = Path(image_dir, image_name)
        if image_file.exists():
            logger.info(
                "Client.build won't build SIF image (file exists: %s).",
                image_file,
            )
            return

        # Make sure a directory to store image exists. If paths are like "/opt/...", the call may fail.
        image_file.parent.mkdir(parents=True, exist_ok=True)

        build_dir = Path(
            build_dir
        )  # Let's assume that build context is the root MLCube directory
        if recipe.startswith("docker://") or recipe.startswith("docker-archive:"):
            # https://sylabs.io/guides/3.0/user-guide/build_a_container.html
            # URI beginning with docker:// to build from Docker Hub
            logger.info(
                "Client.build will build SIF image from docker image (image=%s).",
                recipe,
            )
        else:
            # This must be a recipe file. Make sure it exists.
            if not Path(build_dir, recipe).exists():
                raise IOError(
                    f"SIF recipe file does not exist (path={build_dir}, file={recipe})"
                )
            logger.info(
                "Client.build will build SIF image from recipe file (path=%s, file=%s).",
                build_dir,
                recipe,
            )
        try:
            Shell.run(
                ["cd", str(build_dir), ";"]
                + self.singularity
                + ["build", build_args, str(image_file), recipe]
            )
        except ExecutionError as err:
            raise ExecutionError.mlcube_configure_error(
                self.__class__.__name__,
                "Error occurred while building SIF image. See context for more details.",
                **err.context,
            )

    def run(
        self,
        run_args: str,
        volumes: str,
        image_file: str,
        args: t.List,
        entrypoint: t.Optional[str] = None,
    ) -> None:
        try:
            if entrypoint:
                Shell.run(
                    self.singularity
                    + [
                        "exec",
                        run_args,
                        volumes,
                        image_file,
                        entrypoint,
                        " ".join(args),
                    ]
                )
            else:
                Shell.run(
                    self.singularity
                    + ["run", run_args, volumes, image_file, " ".join(args)]
                )
        except ExecutionError as err:
            raise ExecutionError.mlcube_run_error(
                self.__class__.__name__,
                f"Error occurred while running MLCube task. See context for more details.",
                **err.context,
            )

    def image_spec(self, uri: str) -> ImageSpec:
        if uri.startswith("docker://"):
            return ImageSpec.DOCKER

        if uri.startswith("docker-archive:"):
            return ImageSpec.DOCKER_ARCHIVE

        if not Path(uri).is_file():
            logger.warning(
                "Client.image_spec URI (%s) not a file. Can't identify image spec.", uri
            )
            return ImageSpec.OTHER

        exit_code, _ = Shell.run_and_capture_output(
            self.singularity + ["sif", "header", uri]
        )
        if exit_code == 0:
            return ImageSpec.SINGULARITY


class DockerHubClient:
    """Ad-hoc implementation for interacting with remote docker registries."""

    def __init__(self, singularity_: Client) -> None:
        """
        self.token: t.Optional[str] = None

        if singularity.version.runtime == Runtime.APPTAINER:
            config_paths = [
                Path("~/.apptainer/docker-config.json").expanduser(),
                Path("~/.singularity/docker-config.json").expanduser(),
            ]
        else:
            config_paths = [
                Path("~/.singularity/docker-config.json").expanduser(),
                Path("~/.apptainer/docker-config.json").expanduser(),
            ]
        config_paths.append(Path("~/.docker/config.json").expanduser())

        for config_path in config_paths:
            if not config_path.is_file():
                logger.debug("DockerHubClient.__init__ no such file: %s", config_path.as_posix())
                continue
            with open(config_path, 'rt') as file:
                config = json.load(file)
            if not isinstance(config, dict) or \
                    "auths" not in config or \
                    "https://index.docker.io/v1/" not in config["auths"] or \
                    "auth" not in config["auths"]["https://index.docker.io/v1/"]:
                logger.debug("DockerHubClient.__init__: no docker.io credentials in %s", config_path.as_posix())
                continue
            self.token = config["auths"]["https://index.docker.io/v1/"]["auth"]
            logger.debug("DockerHubClient.__init__: found auth credentials in %s.", config_path.as_posix())
            break
        if not self.token:
            logger.warning(
                "DockerHubClient.__init__: could not credentials to authenticate in docker registry in %s",
                [name.as_posix() for name in config_paths]
            )
        """
        pass

    def get_image_manifest(self, image_name: str) -> t.Dict:
        """Return image manifest pulled from a remote docker registry.
        Args:
            image_name: Docker image name, e.g., docker://mlcommons/mnist:0.0.1
        Returns:
            Dictionary containing image manifest pulled from docker registry.
        """
        user, repository, tag = self.parse_image_name(image_name)
        token = self.get_token(user, repository)

        url = f"https://registry-1.docker.io/v2/{user}/{repository}/manifests/{tag}"
        headers = {
            "Accept": "application/vnd.docker.distribution.manifest.v2+json",
            "Authorization": f"Bearer {token}",
        }
        response = requests.get(url, headers=headers)
        if response.status_code != 200:
            raise ValueError(
                f"Failed to get image manifest (status={response.status_code}, url={url}, response={response.text}"
            )
        return response.json()

    @staticmethod
    def get_token(user: str, repository: str) -> str:
        """Return authentication token for pulling from {user}/{repository} repository.

        Args:
            user: Username in a remote docker registry.
            repository: Repository name in a remote docker registry.
        Returns:
            Access token for pulling from {user}/{repository} repository. It should be used in the Authorization header:
            `"Authorization": f"Bearer {token}"`.
        """
        url = f"https://auth.docker.io/token?service=registry.docker.io&scope=repository:{user}/{repository}:pull"
        response = requests.get(url)
        if response.status_code != 200:
            raise ValueError(
                f"Failed to get token (status={response.status_code}, url={url}, response={response.text}"
            )
        return response.json()["token"]

    @staticmethod
    def parse_image_name(name: str) -> t.Tuple[str, str, str]:
        """Parse image name and return username, repository name and image tag.

        Args:
            name: Image name.
        Returns:
            Tuple containing username, repository name and image tag.
        """
        if name.startswith("docker:"):
            name = name[7:]
        while True:
            if len(name) > 0 and name[0] == "/":
                name = name[1:]
            else:
                break
        name_tag = name.split(":")
        if len(name_tag) != 2:
            raise ValueError(f"Unsupported image name: {name}")
        user_repository = name_tag[0].split("/")
        if len(user_repository) != 2:
            raise ValueError(f"Unsupported image name: {name}")

        return (user_repository[0], user_repository[1], name_tag[1])
