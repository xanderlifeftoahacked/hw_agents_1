import pytest

from helpers import Project


@pytest.fixture
def project(tmp_path):
    return Project(tmp_path)


@pytest.fixture
def make_project(tmp_path):
    def make(**options):
        return Project(tmp_path, **options)

    return make
