import json

from scripts.update_translation_profile_snapshot import (
    SNAPSHOT_CONSTANT,
    canonical_json_hash,
    main,
    read_snapshot_hash,
    update_snapshot_hash,
)


def _write_profile_data(path, value="text"):
    path.write_text(
        json.dumps(
            {
                "qwen": {"profile_b": value, "profile_a": "text"},
                "standard": {"profile_a": "text", "profile_b": value},
            }
        ),
        encoding="utf-8",
    )


def _write_test_file(path, hash_value="0" * 64):
    path.write_text(
        f'{SNAPSHOT_CONSTANT} = "{hash_value}"\n',
        encoding="utf-8",
    )


def test_canonical_json_hash_is_order_stable(tmp_path):
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    _write_profile_data(first)
    second.write_text(
        json.dumps(
            {
                "standard": {"profile_b": "text", "profile_a": "text"},
                "qwen": {"profile_a": "text", "profile_b": "text"},
            }
        ),
        encoding="utf-8",
    )

    assert canonical_json_hash(first) == canonical_json_hash(second)


def test_update_snapshot_hash_replaces_stale_hash(tmp_path):
    profile_data = tmp_path / "translation_profiles.json"
    test_file = tmp_path / "test_translation_prompts.py"
    _write_profile_data(profile_data)
    _write_test_file(test_file)

    new_hash = update_snapshot_hash(profile_data, test_file)

    assert read_snapshot_hash(test_file) == new_hash


def test_main_check_returns_nonzero_for_stale_hash(tmp_path):
    profile_data = tmp_path / "translation_profiles.json"
    test_file = tmp_path / "test_translation_prompts.py"
    _write_profile_data(profile_data)
    _write_test_file(test_file)

    result = main([
        "--check",
        "--profile-data",
        str(profile_data),
        "--test-file",
        str(test_file),
    ])

    assert result == 1


def test_main_check_returns_zero_for_current_hash(tmp_path):
    profile_data = tmp_path / "translation_profiles.json"
    test_file = tmp_path / "test_translation_prompts.py"
    _write_profile_data(profile_data)
    _write_test_file(test_file, canonical_json_hash(profile_data))

    result = main([
        "--check",
        "--profile-data",
        str(profile_data),
        "--test-file",
        str(test_file),
    ])

    assert result == 0
