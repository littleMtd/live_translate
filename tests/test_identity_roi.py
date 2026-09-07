import io
import json
from unittest.mock import patch
from unittest.mock import MagicMock
from types import SimpleNamespace

from PIL import Image

from modules.identity_roi import (
    IdentityRoiStore,
    IdentityRoiCalibrationWindow,
    NormalizedRoi,
    calibration_key,
    crop_identity_roi,
    exact_reviewed_member,
    normalize_identity,
    parse_observed_identity,
)
from modules.profile_context import profile_state


def test_normalized_roi_rejects_malformed_and_out_of_bounds_values():
    assert NormalizedRoi.parse({"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.4})
    for value in (
        None,
        [],
        {"x": 0, "y": 0, "width": 0, "height": 1},
        {"x": -0.1, "y": 0, "width": 0.2, "height": 0.2},
        {"x": 0.9, "y": 0, "width": 0.2, "height": 0.2},
        {"x": "0", "y": 0, "width": 0.2, "height": 0.2},
        {"x": 0, "y": 0, "width": 0.2, "height": 0.2, "extra": 1},
    ):
        assert NormalizedRoi.parse(value) is None


def test_store_keeps_valid_platform_entry_when_siblings_are_malformed(tmp_path):
    path = tmp_path / "identity_rois.json"
    path.write_text(json.dumps({"version": 1, "rois": {
        "soop": {"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.4},
        "chzzk": {"x": [], "y": 0, "width": 1, "height": 1},
    }}), encoding="utf-8")
    store = IdentityRoiStore(path)
    assert store.get("SOOP") == NormalizedRoi(0.1, 0.2, 0.3, 0.4)
    assert store.get("CHZZK") is None


def test_crop_uses_normalized_player_coordinates():
    image = Image.new("RGB", (200, 100), "black")
    for x in range(50, 150):
        for y in range(25, 75):
            image.putpixel((x, y), (255, 255, 255))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=95)
    observation = crop_identity_roi(buffer.getvalue(), NormalizedRoi(.25, .25, .5, .5))
    assert observation is not None
    assert len(observation.thumb) == 64 * 16
    cropped = Image.open(io.BytesIO(observation.jpeg))
    assert cropped.size == (100, 50)


def test_identity_parser_and_lookup_are_exact_and_conservative():
    registry = profile_state.registry
    assert parse_observed_identity('{"identity":"  Ranko  "}') == "Ranko"
    assert normalize_identity("  RANKO ") == "ranko"
    assert exact_reviewed_member("RANKO", registry).marker_id == "url_member_ranko"
    assert exact_reviewed_member("솜망", registry).marker_id == "url_member_sommyang"
    assert exact_reviewed_member("솔망", registry).marker_id == "url_member_sommyang"
    assert exact_reviewed_member("솨먕", registry).marker_id == "url_member_sommyang"
    assert exact_reviewed_member("숨주먹", registry).marker_id == "hades_member_sompunch"
    assert exact_reviewed_member("Ranko fan", registry) is None
    assert exact_reviewed_member("솜", registry) is None
    assert exact_reviewed_member("주먹", registry) is None
    assert exact_reviewed_member([], registry) is None
    assert parse_observed_identity('{"identity":[] }') == ""


def test_platform_aliases_have_separate_layout_keys():
    assert calibration_key("SOOP") == "soop"
    assert calibration_key("CHZZK") == "chzzk"
    assert calibration_key("치지직") == "chzzk"
    assert calibration_key("unsupported") == ""


def test_calibration_snapshot_uses_successful_production_capture_without_writing_image():
    with patch("modules.identity_roi._capture_calibration_image", return_value=("soop", Image.new("RGB", (20, 10)))):
        from modules.identity_roi import calibration_snapshot

        result = calibration_snapshot()
    assert result["platform"] == "soop"
    assert result["image_data_url"].startswith("data:image/jpeg;base64,")


def test_store_save_is_platform_scoped_and_malformed_save_preserves_previous(tmp_path):
    path = tmp_path / "identity_rois.json"
    store = IdentityRoiStore(path)
    soop = store.save("soop", NormalizedRoi(0.1, 0.2, 0.3, 0.4))
    store.save("chzzk", NormalizedRoi(0.2, 0.3, 0.4, 0.2))
    assert store.get("soop") == soop
    assert store.get("chzzk") == NormalizedRoi(0.2, 0.3, 0.4, 0.2)
    try:
        store.save("soop", {"x": 0, "y": 0, "width": 0, "height": 1})
    except ValueError:
        pass
    assert store.get("soop") == soop


def test_native_calibration_starts_from_saved_roi_and_cancel_does_not_persist(tmp_path):
    store = IdentityRoiStore(tmp_path / "identity_rois.json")
    original = store.save("soop", NormalizedRoi(0.1, 0.2, 0.3, 0.4))
    window = IdentityRoiCalibrationWindow(
        "soop", Image.new("RGB", (200, 100)), store=store, show_only=False
    )
    assert window.roi == original
    window.roi = NormalizedRoi(0.4, 0.4, 0.2, 0.2)
    window._root = MagicMock()
    window._cancel()
    assert store.get("soop") == original


def test_native_calibration_move_resize_stays_normalized_and_save_uses_shared_store(tmp_path):
    store = IdentityRoiStore(tmp_path / "identity_rois.json")
    window = IdentityRoiCalibrationWindow(
        "chzzk", Image.new("RGB", (200, 100)), store=store, show_only=False
    )
    window._display_width = 200
    window._display_height = 100
    window._canvas = MagicMock()
    window._rectangle = 1
    window._handle = 2
    window._drag = ("move", 0, 0, window.roi)
    window._move(SimpleNamespace(x=1000, y=1000))
    assert window.roi.x + window.roi.width == 1
    assert window.roi.y + window.roi.height == 1
    window._root = MagicMock()
    window._save()
    assert store.get("chzzk") == window.roi
    assert window.saved is True
