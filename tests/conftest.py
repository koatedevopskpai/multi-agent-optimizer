"""Run the same POSIX harnesses on native Linux and Windows/WSL."""
import os
import shutil
import subprocess

import pytest


@pytest.fixture(scope="session")
def posix_harness_command():
    if os.name == "nt":
        if shutil.which("wsl") is None:
            pytest.skip("WSL is not installed; run POSIX harnesses in Linux CI")
        status = subprocess.run(["wsl", "--status"], capture_output=True, timeout=20)
        if status.returncode != 0:
            pytest.skip("WSL is unavailable to this execution environment; Linux CI required")

    def command(path, *, root=False):
        if os.name == "nt":
            resolved = path.resolve()
            target = "/mnt/" + resolved.drive[0].lower() + str(resolved)[2:].replace("\\", "/")
            return ["wsl", *(["-u", "root"] if root else []), "bash", target]
        bash = shutil.which("bash")
        if bash is None:
            pytest.skip("bash is required for POSIX harnesses")
        if root and os.geteuid() != 0:
            sudo = shutil.which("sudo")
            if sudo is None or subprocess.run([sudo, "-n", "true"], capture_output=True).returncode != 0:
                pytest.skip("Disposable mount harness requires root or passwordless sudo")
            return [sudo, "-n", bash, str(path)]
        return [bash, str(path)]

    return command
