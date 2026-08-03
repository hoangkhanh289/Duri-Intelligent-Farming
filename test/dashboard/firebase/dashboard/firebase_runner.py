import sys
import os
import io
import json
import argparse
import urllib.request
import urllib.parse
from datetime import datetime, timezone
import time

def _force_utf8_streams():
    for _name in ("stdout", "stderr"):
        _stream = getattr(sys, _name, None)
        if _stream is None:
            continue
        if hasattr(_stream, "reconfigure"):
            try:
                _stream.reconfigure(encoding="utf-8", errors="replace")
                continue
            except Exception:
                pass
        try:
            setattr(sys, _name, io.TextIOWrapper(
                _stream.buffer, encoding="utf-8", errors="replace", newline=""
            ))
        except Exception:
            pass

_force_utf8_streams()

import firebase_admin
from firebase_admin import credentials, db

try:
    import joblib
    _JOBLIB_OK = True
except Exception:
    joblib = None
    _JOBLIB_OK = False

DATABASE_URL = "https://sam-team-9c24e-default-rtdb.asia-southeast1.firebasedatabase.app"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CREDENTIAL_PATH = os.path.join(SCRIPT_DIR, "credentials", "serviceAccountKey.json")

SERVER_TIMESTAMP = {".sv": "timestamp"}

LABEL_FILE_CANDIDATES = [
    os.path.join(SCRIPT_DIR, "label.json"),
    os.path.join(SCRIPT_DIR, "..", "label.json"),
    os.path.join(os.getcwd(), "label.json"),
]

FIELD_ALIAS_MAP = {
    "air_humidity": "air_humidity",
    "air_temperature": "air_temp",
    "soil_humidity": "soil_humidity",
    "soil_temperature": "soil_temp",
    "water_storage": "water_storage", # Fixed typo water_torage -> water_storage mapping
}
AVERAGE_FIELDS = list(FIELD_ALIAS_MAP.keys())

MODEL_DIR_CANDIDATES = [
    os.path.join(SCRIPT_DIR, "models"),
    os.path.join(SCRIPT_DIR, "..", "models"),
    os.path.join(os.getcwd(), "models"),
]

MODEL_FEATURE_NAME_VARIANTS = {
    "air_humidity":    ["air humidity", "airhumidity", "do am khong khi", "air_humidity"],
    "air_temperature": ["air temperature", "airtemperature", "nhiet do khong khi", "air_temp", "air_temperature"],
    "soil_humidity":   ["soil humidity", "soilhumidity", "do am dat", "soil_humidity"],
    "soil_temperature":["soil temperature", "soiltemperature", "nhiet do dat", "soil_temp", "soil_temperature"],
    "water_storage":   ["water storage", "waterstorage", "tru nuoc", "water_storage", "water_torage", "rainfall"],
}
GROWTH_STAGE_VARIANTS = ["growth stage", "growthstage", "giai doan sinh truong", "stage", "growth_stage"]

_model_cache = None

def _norm_feature_name(name):
    return "".join(str(name).lower().split())

def find_model_dir():
    for path in MODEL_DIR_CANDIDATES:
        if os.path.isdir(path):
            return os.path.abspath(path)
    return None

def load_trained_models():
    global _model_cache
    if _model_cache is not None:
        return _model_cache
    _model_cache = []
    if not _JOBLIB_OK: return _model_cache
    model_dir = find_model_dir()
    if not model_dir: return _model_cache

    for fname in sorted(os.listdir(model_dir)):
        if not fname.endswith("_meta.json"): continue
        stem = fname[: -len("_meta.json")]
        meta_path = os.path.join(model_dir, fname)
        pkl_path = os.path.join(model_dir, f"{stem}.pkl")
        if not os.path.isfile(pkl_path): continue
        meta = None
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
            model = joblib.load(pkl_path)
            _model_cache.append({"meta": meta, "model": model, "pkl_path": pkl_path})
        except Exception as e:
            _model_cache.append({"meta": {"stem": stem, "note": meta.get("note", "") if meta else ""},
                                  "model": None, "pkl_path": pkl_path, "load_error": str(e)})
    return _model_cache

def resolve_model_feature_value(feature_name, averages_raw, growth_stage):
    norm = _norm_feature_name(feature_name)
    if norm in [_norm_feature_name(v) for v in GROWTH_STAGE_VARIANTS]:
        return float(growth_stage) if growth_stage is not None else None

    for firebase_field, variants in MODEL_FEATURE_NAME_VARIANTS.items():
        if norm in [_norm_feature_name(v) for v in variants]:
            return averages_raw.get(firebase_field)
    return None

def get_decision_rule_text(model, feature_names, sample_row):
    try:
        tree = model.tree_
        node_indicator = model.decision_path([sample_row])
        leaf_id = model.apply([sample_row])[0]
        node_index = node_indicator.indices[node_indicator.indptr[0]: node_indicator.indptr[1]]
        steps = []
        for node_id in node_index:
            if leaf_id == node_id: continue
            feat = feature_names[tree.feature[node_id]]
            threshold = tree.threshold[node_id]
            val = sample_row[tree.feature[node_id]]
            op = "<=" if val <= threshold else ">"
            steps.append(f"{feat} {op} {threshold:.2f}")
        return steps
    except Exception:
        return []

def predict_with_trained_models(averages_raw, growth_stage):
    results = []
    if not _JOBLIB_OK: return results
    models = load_trained_models()
    if not models: return results

    for entry in models:
        meta = entry["meta"]
        model = entry["model"]
        stem = meta.get("stem", "?")
        note = meta.get("note", "")

        if model is None:
            results.append({"group_file": meta.get("file", stem), "stem": stem, "note": note, "error": entry.get("load_error", "Không load được model")})
            continue

        features = meta.get("features", [])
        defaults = meta.get("feature_defaults", {})
        row = []
        used, imputed, missing = [], [], []

        for feat in features:
            val = resolve_model_feature_value(feat, averages_raw, growth_stage)
            if val is not None:
                row.append(val)
                used.append(feat)
            elif feat in defaults:
                row.append(defaults[feat])
                imputed.append(feat)
            else:
                missing.append(feat)

        if missing:
            results.append({"group_file": meta.get("file", stem), "stem": stem, "note": note, "skipped": True, "reason": f"Thiếu feature không thể impute: {', '.join(missing)}"})
            continue

        try:
            pred = model.predict([row])[0]
            proba = None
            if hasattr(model, "predict_proba"):
                probs = model.predict_proba([row])[0]
                proba = round(float(max(probs)), 4)
            label_meaning = meta.get("label_meaning", {}).get(pred, "")
            rule_path = get_decision_rule_text(model, features, row)
            results.append({
                "group_file": meta.get("file", stem), "stem": stem, "note": note,
                "label_id": pred, "label_name": label_meaning, "confidence": proba,
                "features_used": used, "features_imputed": imputed,
                "decision_path": rule_path, "model_accuracy": meta.get("accuracy"),
            })
        except Exception as e:
            results.append({"group_file": meta.get("file", stem), "stem": stem, "note": note, "error": f"{type(e).__name__}: {e}"})
    return results

WEATHER_CACHE_FILE = os.path.join(SCRIPT_DIR, "weather_cache.json")
WEATHER_TTL_SECONDS = 15 * 60

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

def find_label_file():
    for path in LABEL_FILE_CANDIDATES:
        if os.path.isfile(path): return os.path.abspath(path)
    return None

def load_label_config():
    path = find_label_file()
    if not path:
        raise FileNotFoundError("Không tìm thấy label.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f), path

def build_alias_map(col_aliases):
    alias_map = {}
    for col_name, alias in (col_aliases or {}).items():
        normalized = " ".join(str(col_name).split())
        alias_map[normalized] = alias
    return alias_map

_OPS = {
    "eq": lambda a, b: a == b, "ne": lambda a, b: a != b,
    "lt": lambda a, b: a < b, "le": lambda a, b: a <= b,
    "gt": lambda a, b: a > b, "ge": lambda a, b: a >= b,
    "in": lambda a, b: a in b,
    "between": lambda a, b: isinstance(b, list) and len(b) == 2 and b[0] <= a <= b[1],
}

def eval_condition(cond, vals):
    col, op, val = cond["col"], cond["op"], cond["val"]
    if col == "n_over_k_ratio":
        if "n" not in vals or "k" not in vals or vals["k"] == 0: return False
        actual = vals["n"] / vals["k"]
    else:
        if col not in vals: return False
        actual = vals[col]
    fn = _OPS.get(op)
    return fn(actual, val) if fn else False

def eval_conditions_and(conditions, vals):
    return all(eval_condition(c, vals) for c in conditions)

def stage_matches(label_def, vals):
    stages = label_def.get("stage") or []
    if not stages: return True
    cur_stage = vals.get("stage")
    if cur_stage is None: return False
    return int(cur_stage) in [int(s) for s in stages]

def get_all_matching_labels(vals, label_cfg):
    all_labels = label_cfg.get("labels", [])
    matched = []
    for label_def in all_labels:
        if not stage_matches(label_def, vals): continue
        conditions = label_def.get("conditions", [])
        if conditions and eval_conditions_and(conditions, vals):
            matched.append(label_def)
    return matched

def normalize_raw_inputs(raw_inputs, alias_map):
    vals = {}
    for key, val in raw_inputs.items():
        if val is None or str(val).strip() == "": continue
        alias_key = alias_map.get(key, key)
        try:
            vals[alias_key] = float(val)
        except (ValueError, TypeError):
            try: vals[alias_key] = int(val)
            except: vals[alias_key] = val
    return vals

def summarize_label(label_def):
    return {
        "label_id": label_def.get("label_id"),
        "label_name": label_def.get("label_name"),
        "group": label_def.get("group"),
        "severity": label_def.get("severity", "Normal"),
        "description": label_def.get("description", ""),
        "recommendation": label_def.get("recommendation", ""),
    }

def average_node_values(nodes_dict, fields):
    sums = {f: 0.0 for f in fields}
    counts = {f: 0 for f in fields}
    for node_key, node_val in (nodes_dict or {}).items():
        if not isinstance(node_val, dict) or not node_key.startswith("node"):
            continue
        for f in fields:
            # Handle slight variations in keys
            v = node_val.get(f)
            if v is None and f == 'water_storage':
                v = node_val.get('water_torage')
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                sums[f] += v
                counts[f] += 1
    return {f: round(sums[f] / counts[f], 2) for f in fields if counts[f] > 0}

def fetch_weather_api(lat, lon):
    params = urllib.parse.urlencode({
        "latitude": lat,
        "longitude": lon,
        "current": "precipitation,temperature_2m,relative_humidity_2m",
        "daily": "precipitation_probability_max",
        "timezone": "auto",
    })
    url = f"{FORECAST_URL}?{params}"
    with urllib.request.urlopen(url, timeout=8) as r:
        data = json.loads(r.read().decode("utf-8"))
    current = data.get("current", {})
    daily = data.get("daily", {})
    
    rain_prob = daily.get("precipitation_probability_max", [0])[0]
    if rain_prob is None: rain_prob = 0
    
    return {
        "rainfall_mm": float(current.get("precipitation", 0.0)),
        "outdoor_temperature": float(current.get("temperature_2m", 0.0)),
        "outdoor_humidity": float(current.get("relative_humidity_2m", 0.0)),
        "rain_probability": float(rain_prob),
        "observed_at": current.get("time")
    }

def get_weather_for_coords(lat, lng):
    cache = {}
    if os.path.isfile(WEATHER_CACHE_FILE):
        try:
            with open(WEATHER_CACHE_FILE, "r", encoding="utf-8") as f:
                cache = json.load(f)
        except Exception:
            cache = {}

    cache_key = f"{round(lat,4)}_{round(lng,4)}"
    now = datetime.now(timezone.utc)
    
    if cache_key in cache:
        c_data = cache[cache_key]
        if c_data.get("fetched_at") and not c_data.get("error"):
            try:
                fetched_dt = datetime.fromisoformat(c_data["fetched_at"])
                if (now - fetched_dt).total_seconds() < WEATHER_TTL_SECONDS:
                    return c_data
            except Exception:
                pass

    result = {"fetched_at": now.isoformat(), "source": "open-meteo.com", "lat": lat, "lng": lng}
    try:
        w_data = fetch_weather_api(lat, lng)
        result.update(w_data)
        result["error"] = None
    except Exception as e:
        result["rainfall_mm"] = None
        result["outdoor_temperature"] = None
        result["outdoor_humidity"] = None
        result["rain_probability"] = None
        result["error"] = str(e)

    cache[cache_key] = result
    try:
        with open(WEATHER_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
    except Exception:
        pass
    return result

def run_label_analysis(garden_data):
    label_cfg, label_path = load_label_config()
    alias_map = build_alias_map(label_cfg.get("_meta", {}).get("col_aliases", {}))

    growth_stage = garden_data.get("growth_stage")
    coords = garden_data.get("coordinates", {})
    lat = coords.get("lat")
    lng = coords.get("lng")

    averages_raw = average_node_values(garden_data, AVERAGE_FIELDS)
    averages_aliased = {FIELD_ALIAS_MAP[k]: v for k, v in averages_raw.items()}

    weather = {}
    if lat is not None and lng is not None:
        weather = get_weather_for_coords(lat, lng)

    raw_inputs = dict(averages_aliased)
    if growth_stage is not None:
        raw_inputs["stage"] = float(growth_stage)
    
    if weather.get("rainfall_mm") is not None:
        raw_inputs["rainfall"] = weather["rainfall_mm"]
        raw_inputs["rain_probability"] = weather.get("rain_probability")
        raw_inputs["outdoor_temp"] = weather.get("outdoor_temperature")
        raw_inputs["outdoor_humidity"] = weather.get("outdoor_humidity")

    vals = normalize_raw_inputs(raw_inputs, alias_map)
    matched_defs = get_all_matching_labels(vals, label_cfg)
    matched = [summarize_label(d) for d in matched_defs]

    model_predictions = predict_with_trained_models(averages_raw, growth_stage)

    return {
        "label_file": label_path,
        "weather": weather,
        "node_averages": averages_raw,
        "growth_stage": growth_stage,
        "analyzed_parameters": list(vals.keys()),
        "matched": matched,
        "model_predictions": model_predictions,
    }

MAX_HISTORY_RECORDS = 100
AI_MODEL_VERSION = "sam-ai-v1.3.0"
SERVER_VERSION = "1.3.0"
API_VERSION = "v1"
ACTUATOR_KEYS = ["pump", "valve", "fan", "mist", "fertilizer", "light", "buzzer"]
SEVERITY_HEALTH_PENALTY = {"CRITICAL": 30, "WARNING": 10, "NORMAL": 0}


def now_ms():
    return int(time.time() * 1000)


def compute_health_score(matched_labels):
    score = 100
    for lb in matched_labels:
        sev = (lb.get("severity") or "NORMAL").upper()
        score -= SEVERITY_HEALTH_PENALTY.get(sev, 0)
    return max(0, min(100, score))


def status_from_score(score):
    if score >= 80:
        return "GOOD", "#4CAF50", "LOW"
    if score >= 50:
        return "WARNING", "#FF9800", "MEDIUM"
    return "CRITICAL", "#F44336", "HIGH"


def build_labels_dict(matched_labels, ts):
    labels = {}
    for i, lb in enumerate(matched_labels, start=1):
        labels[f"label_{ts}_{i}"] = {**lb, "matched_time": ts}
    return labels


def build_model_predictions_dict(model_predictions, ts):
    preds = {}
    for i, mp in enumerate(model_predictions, start=1):
        preds[f"model_{ts}_{i}"] = {
            "model_name": mp.get("stem") or mp.get("group_file") or "unknown",
            "predicted_label": mp.get("label_name") or mp.get("label_id"),
            "confidence": mp.get("confidence"),
            "model_accuracy": mp.get("model_accuracy"),
            "decision_path": mp.get("decision_path", []),
            "features_used": mp.get("features_used", []),
            "features_imputed": mp.get("features_imputed", []),
            "note": mp.get("note") or mp.get("error") or mp.get("reason") or "",
        }
    return preds


def build_alerts_from_labels(matched_labels, ts):
    alerts = {}
    idx = 1
    for lb in matched_labels:
        sev = (lb.get("severity") or "NORMAL").upper()
        if sev == "NORMAL":
            continue
        aid = f"alert_{ts}_{idx}"
        alerts[aid] = {
            "id": aid,
            "severity": sev,
            "title": lb.get("label_name", ""),
            "message": lb.get("description", ""),
            "recommendation": lb.get("recommendation", ""),
            "created_at": ts,
            "acknowledged": False,
            "resolved": False,
        }
        idx += 1
    return alerts


def build_recommendations_from_labels(matched_labels, ts):
    priority_map = {"CRITICAL": "HIGH", "WARNING": "MEDIUM", "NORMAL": "LOW"}
    recs = {}
    idx = 1
    for lb in matched_labels:
        rec_text = lb.get("recommendation")
        if not rec_text:
            continue
        rid = f"rec_{ts}_{idx}"
        recs[rid] = {
            "priority": priority_map.get((lb.get("severity") or "NORMAL").upper(), "LOW"),
            "action": rec_text,
            "reason": lb.get("description", ""),
            "estimated_effect": "",
            "generated_at": ts,
        }
        idx += 1
    return recs


def _actuator(enable, command, value=0, mode="auto", duration=0, reason="", priority="NORMAL", ts=None):
    return {
        "enable": enable, "command": command, "value": value, "mode": mode,
        "duration": duration, "reason": reason, "priority": priority,
        "created_at": ts, "executed": False, "executed_time": None,
    }


def decide_actuators(averages_raw, matched_labels, ts):
    soil_h = averages_raw.get("soil_humidity")
    air_t = averages_raw.get("air_temperature")
    air_h = averages_raw.get("air_humidity")
    has_critical = any((lb.get("severity") or "").upper() == "CRITICAL" for lb in matched_labels)

    actuators = {}

    if soil_h is not None and soil_h < 55:
        actuators["pump"] = _actuator(True, "ON", 0, "auto", 300, "Độ ẩm đất thấp", "HIGH", ts)
        actuators["valve"] = _actuator(True, "ON", 0, "auto", 300, "Mở van cấp nước cho bơm", "HIGH", ts)
    else:
        actuators["pump"] = _actuator(False, "OFF", 0, "auto", 0, "Độ ẩm đất đã đủ", "NORMAL", ts)
        actuators["valve"] = _actuator(False, "OFF", 0, "auto", 0, "", "NORMAL", ts)

    if air_t is not None and air_t >= 30:
        actuators["fan"] = _actuator(True, "ON", 80, "auto", 900, "Nhiệt độ không khí cao", "HIGH", ts)
    elif air_t is not None and air_t >= 27:
        actuators["fan"] = _actuator(True, "ON", 50, "auto", 600, "Nhiệt độ hơi cao", "NORMAL", ts)
    else:
        actuators["fan"] = _actuator(False, "OFF", 0, "auto", 0, "", "NORMAL", ts)

    if air_h is not None and air_h < 55:
        actuators["mist"] = _actuator(True, "ON", 0, "auto", 300, "Độ ẩm không khí thấp", "NORMAL", ts)
    else:
        actuators["mist"] = _actuator(False, "OFF", 0, "auto", 0, "", "NORMAL", ts)

    actuators["fertilizer"] = _actuator(False, "OFF", 0, "manual", 0, "Chưa tới chu kỳ bón phân", "LOW", ts)
    actuators["light"] = _actuator(True, "ON", 80, "auto", 3600, "Bổ sung ánh sáng theo lịch", "NORMAL", ts)
    actuators["buzzer"] = _actuator(
        has_critical, "ON" if has_critical else "OFF", 1 if has_critical else 0,
        "auto", 5 if has_critical else 0,
        "Cảnh báo nghiêm trọng" if has_critical else "", "HIGH" if has_critical else "NORMAL", ts,
    )
    return actuators


def merge_actuators_preserve_ack(new_actuators, existing_actuators):
    """Giữ lại executed/executed_time do ESP32 báo về, nếu lệnh mới giống lệnh cũ."""
    existing_actuators = existing_actuators or {}
    merged = {}
    for key, val in new_actuators.items():
        old = existing_actuators.get(key)
        merged_val = dict(val)
        if isinstance(old, dict) and old.get("command") == val.get("command") and old.get("value") == val.get("value"):
            merged_val["executed"] = old.get("executed", val["executed"])
            merged_val["executed_time"] = old.get("executed_time", val["executed_time"])
        merged[key] = merged_val
    return merged


def build_analysis_payload(g_id, garden_data, analysis_raw, existing_analysis):
    """Gộp kết quả run_label_analysis() thành đúng schema garden{N}/analysis."""
    ts = now_ms()
    matched = analysis_raw.get("matched", [])
    model_predictions_raw = analysis_raw.get("model_predictions", [])
    averages_raw = analysis_raw.get("node_averages", {})
    weather = analysis_raw.get("weather", {})
    existing_analysis = existing_analysis or {}

    labels_dict = build_labels_dict(matched, ts)
    model_predictions_dict = build_model_predictions_dict(model_predictions_raw, ts)
    alerts_dict = build_alerts_from_labels(matched, ts)
    recommendations_dict = build_recommendations_from_labels(matched, ts)

    new_actuators = decide_actuators(averages_raw, matched, ts)
    actuators_dict = merge_actuators_preserve_ack(new_actuators, existing_analysis.get("actuators"))

    health_score = compute_health_score(matched)
    overall_status, color, danger_level = status_from_score(health_score)
    dashboard = {
        "health_score": health_score,
        "overall_status": overall_status,
        "color": color,
        "icon": "leaf-check" if overall_status == "GOOD" else ("leaf-warning" if overall_status == "WARNING" else "leaf-alert"),
        "danger_level": danger_level,
        "active_alerts": len(alerts_dict),
        "recommendation_count": len(recommendations_dict),
        "last_analysis": ts,
    }

    # ---- history: append + trim còn 100 bản ghi gần nhất ----
    existing_history = existing_analysis.get("history") or {}
    history_entry = {
        "timestamp": ts,
        "labels": [lb.get("label_id") for lb in matched],
        "averages": averages_raw,
        "alerts": list(alerts_dict.keys()),
        "weather": {"rainfall_mm": weather.get("rainfall_mm"), "outdoor_temperature": weather.get("outdoor_temperature")},
        "predictions": [mp.get("predicted_label") for mp in model_predictions_dict.values()],
        "actions_sent": [f"{k}:{v['command']}" for k, v in actuators_dict.items() if v.get("enable")],
    }
    merged_history = dict(existing_history)
    merged_history[f"hist_{ts}"] = history_entry
    if len(merged_history) > MAX_HISTORY_RECORDS:
        oldest_first = sorted(merged_history.keys(), key=lambda k: merged_history[k].get("timestamp", 0))
        for old_key in oldest_first[: len(merged_history) - MAX_HISTORY_RECORDS]:
            merged_history.pop(old_key, None)

    # ---- statistics: cộng dồn từ giá trị cũ ----
    existing_stats = existing_analysis.get("statistics") or {}
    critical_count = sum(1 for lb in matched if (lb.get("severity") or "").upper() == "CRITICAL")
    warning_count = sum(1 for lb in matched if (lb.get("severity") or "").upper() == "WARNING")
    is_normal_run = critical_count == 0 and warning_count == 0
    prev_runs = existing_stats.get("_run_count", 0)
    prev_avg_score = existing_stats.get("average_health_score", health_score)
    new_runs = prev_runs + 1
    new_avg_score = round(((prev_avg_score * prev_runs) + health_score) / new_runs, 2)

    statistics = {
        "total_alerts": existing_stats.get("total_alerts", 0) + len(alerts_dict),
        "critical_alerts": existing_stats.get("critical_alerts", 0) + critical_count,
        "warning_alerts": existing_stats.get("warning_alerts", 0) + warning_count,
        "normal_days": existing_stats.get("normal_days", 0) + (1 if is_normal_run else 0),
        "last_irrigation": ts if actuators_dict.get("pump", {}).get("enable") else existing_stats.get("last_irrigation"),
        "last_fertilizer": ts if actuators_dict.get("fertilizer", {}).get("enable") else existing_stats.get("last_fertilizer"),
        "average_health_score": new_avg_score,
        "_run_count": new_runs,
    }

    summary = {
        "analyzed_at": ts,
        "updated_at": ts,
        "status": "completed",
        "model_version": AI_MODEL_VERSION,
        "data_source": ",".join(k for k in garden_data.keys() if k.startswith("node")),
        "processing_time_ms": analysis_raw.get("processing_time_ms", 0),
        "overall_confidence": analysis_raw.get("overall_confidence", 1.0),
    }

    communication = {
        "server_version": SERVER_VERSION,
        "analysis_id": f"an_{ts}_{g_id}",
        "firebase_timestamp": SERVER_TIMESTAMP,
        "esp32_sync": True,
        "web_sync": True,
        "api_version": API_VERSION,
    }

    return {
        "summary": summary,
        "averages": averages_raw,
        "weather": weather,
        "labels": labels_dict,
        "model_predictions": model_predictions_dict,
        "alerts": alerts_dict,
        "recommendations": recommendations_dict,
        "actuators": actuators_dict,
        "dashboard": dashboard,
        "history": merged_history,
        "statistics": statistics,
        "communication": communication,
    }


def push_analysis_to_firebase(g_id, analysis_payload):
    try:
        db.reference(f"{g_id}/analysis").set(analysis_payload)
    except Exception as e:
        stream_event({"type": "write-error", "garden": g_id, "error": f"{type(e).__name__}: {e}"})


def return_json(data):
    print(json.dumps(data, ensure_ascii=False), flush=True)
    sys.exit(0)

def error_json(message):
    return_json({"error": message})

def stream_event(data):
    print(json.dumps(data, ensure_ascii=False), flush=True)

def apply_event_to_state(state, event_path, data, event_type):
    parts = [p for p in event_path.split("/") if p]
    if not parts:
        if event_type == "put":
            state.clear()
            if isinstance(data, dict): state.update(data)
        else:
            if isinstance(data, dict):
                for k, v in data.items(): state[k] = v
        return

    top = parts[0]
    if len(parts) == 1:
        if event_type == "put":
            if data is None: state.pop(top, None)
            else: state[top] = data
        else:
            node = state.get(top)
            if not isinstance(node, dict): node = {}
            if isinstance(data, dict): node.update(data)
            state[top] = node
    else:
        node = state.get(top)
        if not isinstance(node, dict):
            node = {}
            state[top] = node
        cursor = node
        for p in parts[1:-1]:
            nxt = cursor.get(p)
            if not isinstance(nxt, dict):
                nxt = {}
                cursor[p] = nxt
            cursor = nxt
        cursor[parts[-1]] = data

def init_firebase():
    if not firebase_admin._apps:
        if not os.path.isfile(CREDENTIAL_PATH):
            raise FileNotFoundError(f"Không tìm thấy service account key: {CREDENTIAL_PATH}")
        cred = credentials.Certificate(CREDENTIAL_PATH)
        firebase_admin.initialize_app(cred, {"databaseURL": DATABASE_URL})

def format_time_field(value):
    if isinstance(value, dict) and isinstance(value.get("time"), (int, float)):
        value = {**value}
        value["time_readable"] = datetime.fromtimestamp(
            value["time"] / 1000, tz=timezone.utc
        ).isoformat()
    return value

def process_gardens_state(state, write_to_firebase=True):
    gardens_result = {}
    for g_id, g_data in state.items():
        if not isinstance(g_data, dict) or not g_id.startswith("garden"): continue
        formatted_garden = {}
        for k, v in g_data.items():
            if k in ("analysis", "_analysis_payload_cache"):
                continue
            if k.startswith("node"):
                formatted_garden[k] = format_time_field(v)
            else:
                formatted_garden[k] = v

        cached_payload = g_data.get("_analysis_payload_cache")
        if isinstance(cached_payload, dict):
            existing_analysis = cached_payload
        elif isinstance(g_data.get("analysis"), dict):
            existing_analysis = g_data.get("analysis")
        else:
            existing_analysis = {}

        try:
            # analysis_raw = format cũ (matched/model_predictions là array) — đây là
            # hợp đồng dữ liệu mà firebase_ui.html (chạy local qua runPython/IPC) đang
            # cần, nên KHÔNG đổi format này.
            analysis_raw = run_label_analysis(formatted_garden)

            # analysis_payload = schema 12 nhóm mới, chỉ dùng để GHI vào Firebase
            # (garden{N}/analysis) cho ESP32 và các Web khác đọc trực tiếp từ DB.
            analysis_payload = build_analysis_payload(g_id, formatted_garden, analysis_raw, existing_analysis)

            if write_to_firebase:
                push_analysis_to_firebase(g_id, analysis_payload)
                # Cập nhật state cục bộ ngay (dưới key riêng) để lần chạy kế tiếp merge
                # đúng history/statistics/actuator-ack mới nhất, không phải chờ event echo về.
                state[g_id]["_analysis_payload_cache"] = analysis_payload

            gardens_result[g_id] = {
                "data": formatted_garden,
                "analysis": analysis_raw
            }
        except Exception as e:
            gardens_result[g_id] = {
                "data": formatted_garden,
                "analysis": {"ok": False, "error": f"{type(e).__name__}: {e}"}
            }
    return gardens_result

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--action", required=True)
    args = parser.parse_args()

    try:
        init_firebase()

        if args.action == "stream":
            state = {}
            def on_event(event):
                try:
                    apply_event_to_state(state, event.path, event.data, event.event_type)

                    parts = [p for p in event.path.split("/") if p]
                    # Nếu event chỉ đụng tới nhánh analysis (do chính server ghi, hoặc
                    # ESP32 cập nhật executed/executed_time), không cần chạy lại phân tích
                    # để tránh vòng lặp ghi vô hạn. Vẫn coi nó là cập nhật hợp lệ trong state.
                    touches_only_analysis = len(parts) >= 2 and parts[1] == "analysis"

                    if touches_only_analysis:
                        payload = {"type": "ack-update", "path": event.path}
                        stream_event(payload)
                    else:
                        processed_gardens = process_gardens_state(state)
                        payload = {"type": "update", "gardens": processed_gardens}
                        stream_event(payload)
                except Exception as cb_err:
                    stream_event({"type": "stream-error", "error": str(cb_err)})

            stream_event({"type": "ready", "database_url": DATABASE_URL})
            db.reference("/").listen(on_event)
            
        elif args.action == "get-all":
            state = db.reference("/").get() or {}
            processed_gardens = process_gardens_state(state)
            return_json({"type": "update", "gardens": processed_gardens})

        else:
            error_json(f"Unknown action: {args.action}")

    except Exception as e:
        error_json(f"{type(e).__name__}: {str(e)}")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)