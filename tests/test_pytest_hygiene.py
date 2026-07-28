from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_pytest_cache_and_tmp_path_stay_outside_project(pytestconfig, tmp_path):
    cache_dir = pytestconfig.cache._cachedir.resolve()

    assert not cache_dir.is_relative_to(PROJECT_ROOT)
    assert not tmp_path.resolve().is_relative_to(PROJECT_ROOT)
