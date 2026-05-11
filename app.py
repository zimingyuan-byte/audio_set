import os
import re
import csv
from datetime import datetime
from functools import wraps
from io import BytesIO, StringIO
from zipfile import ZIP_DEFLATED, ZipFile

import click
import yaml
from flask import (
    Flask,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text
from sqlalchemy import UniqueConstraint, and_, func, or_
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.dialects.mysql import LONGBLOB
from sqlalchemy.inspection import inspect
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class RecordSession(db.Model):
    __tablename__ = "record_sessions"

    id = db.Column(db.Integer, primary_key=True)
    speaker_id = db.Column(db.String(128), unique=True, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class AudioRecord(db.Model):
    __tablename__ = "audio_records"
    __table_args__ = (
        UniqueConstraint(
            "speaker_id",
            "text_content",
            "round_index",
            "sample_rate",
            name="uq_record_key",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    session_id = db.Column(db.Integer, db.ForeignKey("record_sessions.id"), nullable=False)
    speaker_id = db.Column(db.String(128), nullable=False, index=True)
    text_content = db.Column(db.String(255), nullable=False)
    round_index = db.Column(db.Integer, nullable=False)
    sample_rate = db.Column(db.Integer, nullable=False)
    bit_depth = db.Column(db.Integer, nullable=False)
    channels = db.Column(db.Integer, nullable=False)
    filename = db.Column(db.String(512), nullable=False)
    audio_data = db.Column(LONGBLOB, nullable=False)
    duration_seconds = db.Column(db.Float, nullable=False, default=0.0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


def load_yaml_config(config_path: str = "config.yaml") -> dict:
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"配置文件不存在: {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    texts = raw.get("recording", {}).get("texts", ["123", "abc"])
    if not isinstance(texts, list):
        texts = ["123", "abc"]
    texts = [str(t) for t in texts]

    rounds_raw = raw.get("recording", {}).get("rounds", [])
    rounds = normalize_rounds_by_texts(texts, rounds_raw)

    cfg = {
        "app": {
            "secret_key": raw.get("app", {}).get("secret_key", "change-me-in-production"),
            "host": raw.get("app", {}).get("host", "0.0.0.0"),
            "port": raw.get("app", {}).get("port", 5000),
            "debug": raw.get("app", {}).get("debug", True),
        },
        "database": {
            "uri": raw.get("database", {}).get("uri", ""),
            "host": raw.get("database", {}).get("host", "127.0.0.1"),
            "port": raw.get("database", {}).get("port", 3306),
            "name": raw.get("database", {}).get("name", "audio_set"),
            "user": raw.get("database", {}).get("user", "root"),
            "password": raw.get("database", {}).get("password", "root"),
            "charset": raw.get("database", {}).get("charset", "utf8mb4"),
        },
        "recording": {
            "texts": texts,
            "rounds": rounds,
            "sample_rate_1": int(raw.get("recording", {}).get("sample_rate_1", 32000)),
            "sample_rate_2": int(raw.get("recording", {}).get("sample_rate_2", 16000)),
            "bit_depth": int(raw.get("recording", {}).get("bit_depth", 16)),
            "channels": int(raw.get("recording", {}).get("channels", 1)),
        },
    }
    return cfg


def build_db_uri(cfg: dict) -> str:
    db_cfg = cfg["database"]
    if db_cfg["uri"]:
        return db_cfg["uri"]
    return (
        f"mysql+pymysql://{db_cfg['user']}:{db_cfg['password']}@"
        f"{db_cfg['host']}:{db_cfg['port']}/{db_cfg['name']}?charset={db_cfg['charset']}"
    )


def login_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped_view


def sanitize_for_filename(value: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "_", value).strip() or "text"


def normalize_rounds_by_texts(texts: list, rounds_raw) -> list:
    texts_len = len(texts)
    if texts_len == 0:
        return []

    if isinstance(rounds_raw, (int, float, str)):
        try:
            r = int(rounds_raw)
        except ValueError:
            r = 1
        if r <= 0:
            r = 1
        return [r] * texts_len

    if rounds_raw is None:
        return [1] * texts_len

    if not isinstance(rounds_raw, list):
        return [1] * texts_len

    if len(rounds_raw) == 0:
        return [1] * texts_len

    if len(rounds_raw) > texts_len:
        raise ValueError("recording.rounds 长度必须小于等于 recording.texts 长度")

    rounds = []
    for i in range(texts_len):
        if i < len(rounds_raw) and rounds_raw[i] not in (None, ""):
            try:
                n = int(rounds_raw[i])
            except (TypeError, ValueError):
                n = 1
            if n <= 0:
                n = 1
            rounds.append(n)
        else:
            rounds.append(1)
    return rounds


def get_wav_duration_seconds(wav_bytes: bytes) -> float:
    # 更稳健地解析 WAV：扫描 fmt/data chunk，而不是固定偏移。
    if len(wav_bytes) < 12 or wav_bytes[0:4] != b"RIFF" or wav_bytes[8:12] != b"WAVE":
        return 0.0

    byte_rate = 0
    data_size = 0
    pos = 12
    total = len(wav_bytes)
    while pos + 8 <= total:
        chunk_id = wav_bytes[pos : pos + 4]
        chunk_size = int.from_bytes(wav_bytes[pos + 4 : pos + 8], byteorder="little", signed=False)
        chunk_data_start = pos + 8
        chunk_data_end = min(chunk_data_start + chunk_size, total)

        if chunk_id == b"fmt " and chunk_data_start + 16 <= total:
            # byte_rate 位于 fmt chunk 的偏移 8..12
            byte_rate = int.from_bytes(
                wav_bytes[chunk_data_start + 8 : chunk_data_start + 12],
                byteorder="little",
                signed=False,
            )
        elif chunk_id == b"data":
            data_size = max(0, chunk_data_end - chunk_data_start)
            break

        # WAV chunk 按 2 字节对齐
        pos = chunk_data_start + chunk_size + (chunk_size % 2)

    if byte_rate <= 0 or data_size <= 0:
        return 0.0
    return round(data_size / byte_rate, 3)


def get_wav_duration_ms(wav_bytes: bytes) -> int:
    return int(round(get_wav_duration_seconds(wav_bytes) * 1000))


def get_recording_progress(speaker_id: str, rec_cfg: dict) -> dict:
    texts = rec_cfg["texts"]
    rounds_by_text = rec_cfg["rounds"]
    rate_1 = int(rec_cfg["sample_rate_1"])
    rate_2 = int(rec_cfg["sample_rate_2"])
    expected_rates = {rate_1, rate_2}
    expected_total = sum(rounds_by_text) * len(expected_rates)

    rows = (
        AudioRecord.query.with_entities(
            AudioRecord.text_content,
            AudioRecord.round_index,
            AudioRecord.sample_rate,
        )
        .filter_by(speaker_id=speaker_id)
        .all()
    )
    done_count = len(rows)
    grouped = {}
    for row in rows:
        key = (row.text_content, row.round_index)
        grouped.setdefault(key, set()).add(row.sample_rate)

    next_text = None
    next_round = None
    next_text_index = None
    complete = True
    for text_idx, text in enumerate(texts):
        text_rounds = rounds_by_text[text_idx] if text_idx < len(rounds_by_text) else 1
        for round_idx in range(1, text_rounds + 1):
            done_rates = grouped.get((text, round_idx), set())
            if done_rates != expected_rates:
                complete = False
                next_text = text
                next_round = round_idx
                next_text_index = text_idx
                break
        if not complete:
            break

    completion = 0.0
    if expected_total > 0:
        completion = round(done_count * 100.0 / expected_total, 1)

    return {
        "speaker_id": speaker_id,
        "expected_total": expected_total,
        "saved_total": done_count,
        "completion_percent": completion,
        "complete": complete,
        "next_text": next_text,
        "next_round": next_round,
        "next_text_index": next_text_index,
    }


def get_session_groups(rec_cfg: dict):
    expected_total = sum(rec_cfg["rounds"]) * 2
    count_rows = (
        db.session.query(
            AudioRecord.speaker_id.label("speaker_id"),
            func.count(AudioRecord.id).label("total_count"),
        )
        .group_by(AudioRecord.speaker_id)
        .all()
    )
    count_map = {row.speaker_id: int(row.total_count) for row in count_rows}

    sessions = RecordSession.query.order_by(RecordSession.speaker_id.asc()).all()
    user_ids = {s.user_id for s in sessions}
    user_rows = User.query.filter(User.id.in_(user_ids)).all() if user_ids else []
    user_map = {u.id: u.username for u in user_rows}

    groups = []
    incomplete_groups = []
    for s in sessions:
        total_count = count_map.get(s.speaker_id, 0)
        completion_percent = round((total_count * 100.0 / expected_total), 1) if expected_total else 0.0
        item = {
            "speaker_id": s.speaker_id,
            "username": user_map.get(s.user_id, f"user-{s.user_id}"),
            "total_count": total_count,
            "completion_percent": completion_percent,
            "is_complete": total_count >= expected_total,
        }
        groups.append(item)
        if not item["is_complete"]:
            incomplete_groups.append(item)

    return groups, incomplete_groups, expected_total


def build_download_rows(rec_cfg: dict):
    rounds_by_text = rec_cfg["rounds"]
    sessions = RecordSession.query.order_by(RecordSession.speaker_id.asc()).all()
    count_rows = (
        db.session.query(
            AudioRecord.speaker_id.label("speaker_id"),
            AudioRecord.text_content.label("text_content"),
            func.count(AudioRecord.id).label("total_count"),
        )
        .group_by(AudioRecord.speaker_id, AudioRecord.text_content)
        .all()
    )
    count_map = {(row.speaker_id, row.text_content): int(row.total_count) for row in count_rows}

    rows = []
    for s in sessions:
        for idx, txt in enumerate(rec_cfg["texts"]):
            expected_per_text = (rounds_by_text[idx] if idx < len(rounds_by_text) else 1) * 2
            total = count_map.get((s.speaker_id, txt), 0)
            if total >= expected_per_text:
                status = "已完成"
            elif total > 0:
                status = "未完成"
            else:
                status = "未录制"
            rows.append(
                {
                    "speaker_id": s.speaker_id,
                    "text_content": txt,
                    "total_count": total,
                    "expected_per_text": expected_per_text,
                    "status": status,
                    "selectable": total > 0,
                    "unit_key": f"{s.speaker_id}|||{txt}",
                }
            )
    return rows


def create_app() -> Flask:
    cfg = load_yaml_config()
    app = Flask(__name__)
    app.config["SECRET_KEY"] = cfg["app"]["secret_key"]
    app.config["SQLALCHEMY_DATABASE_URI"] = build_db_uri(cfg)
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024  # 32MB
    app.config["RECORDING_CFG"] = cfg["recording"]

    db.init_app(app)

    @app.errorhandler(Exception)
    def handle_unexpected_error(exc):
        # API 路径统一返回 JSON，避免前端收到 HTML 后出现 JSON 解析报错。
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "message": f"服务器异常：{str(exc)}"}), 500
        raise exc

    @app.cli.command("init-db")
    def init_db_command():
        with app.app_context():
            db.create_all()
        click.echo("数据库表已创建/更新。")

    @app.route("/")
    def index():
        if "user_id" in session:
            return redirect(url_for("record"))
        return redirect(url_for("login"))

    @app.route("/register", methods=["GET", "POST"])
    def register():
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "").strip()

            if not username or not password:
                flash("用户名和密码不能为空。", "error")
                return redirect(url_for("register"))

            existing = User.query.filter_by(username=username).first()
            if existing:
                flash("用户名已存在，请更换。", "error")
                return redirect(url_for("register"))

            user = User(username=username, password_hash=generate_password_hash(password))
            db.session.add(user)
            db.session.commit()
            flash("注册成功，请登录。", "success")
            return redirect(url_for("login"))

        return render_template("register.html")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "").strip()
            user = User.query.filter_by(username=username).first()

            if not user or not check_password_hash(user.password_hash, password):
                flash("用户名或密码错误。", "error")
                return redirect(url_for("login"))

            session["user_id"] = user.id
            session["username"] = user.username
            flash("登录成功。", "success")
            return redirect(url_for("record"))

        return render_template("login.html")

    @app.route("/logout")
    @login_required
    def logout():
        session.clear()
        flash("已退出登录。", "success")
        return redirect(url_for("login"))

    @app.route("/record")
    @login_required
    def record():
        recording_cfg = app.config["RECORDING_CFG"]
        initial_speaker_id = request.args.get("speaker_id", "").strip()
        _, incomplete_groups, _ = get_session_groups(recording_cfg)
        return render_template(
            "record.html",
            recording_cfg=recording_cfg,
            initial_speaker_id=initial_speaker_id,
            incomplete_groups=incomplete_groups,
        )

    @app.post("/api/start-session")
    @login_required
    def start_session():
        payload = request.get_json(silent=True) or {}
        speaker_id = (payload.get("speaker_id") or "").strip()
        if not speaker_id:
            return jsonify({"ok": False, "message": "ID 不能为空。"}), 400

        rec_session = RecordSession.query.filter_by(speaker_id=speaker_id).first()
        created = False
        if not rec_session:
            rec_session = RecordSession(speaker_id=speaker_id, user_id=session["user_id"])
            db.session.add(rec_session)
            db.session.commit()
            created = True

        progress = get_recording_progress(speaker_id, app.config["RECORDING_CFG"])
        if progress["complete"]:
            return (
                jsonify({"ok": False, "message": "该 ID 已完成全部录制，无需继续。", **progress}),
                409,
            )

        msg = "ID 校验通过，开始录制。" if created else "继续未完成录制。"
        return jsonify({"ok": True, "message": msg, **progress})

    @app.get("/api/session-progress")
    @login_required
    def session_progress():
        speaker_id = request.args.get("speaker_id", "").strip()
        if not speaker_id:
            return jsonify({"ok": False, "message": "ID 不能为空。"}), 400
        rec_session = RecordSession.query.filter_by(speaker_id=speaker_id).first()
        if not rec_session:
            return jsonify({"ok": False, "message": "ID 不存在。"}), 404
        progress = get_recording_progress(speaker_id, app.config["RECORDING_CFG"])
        return jsonify({"ok": True, **progress})

    @app.get("/api/incomplete-ids")
    @login_required
    def incomplete_ids():
        _, incomplete_groups, _ = get_session_groups(app.config["RECORDING_CFG"])
        return jsonify({"ok": True, "incomplete_groups": incomplete_groups})

    @app.post("/api/save-recording")
    @login_required
    def save_recording():
        speaker_id = request.form.get("speaker_id", "").strip()
        text_content = request.form.get("text", "").strip()
        try:
            round_index = int(request.form.get("round_index", "0"))
            sample_rate = int(request.form.get("sample_rate", "0"))
        except ValueError:
            return jsonify({"ok": False, "message": "轮次或采样率格式错误。"}), 400
        upload = request.files.get("audio")
        rec_cfg = app.config["RECORDING_CFG"]
        expected_rates = {rec_cfg["sample_rate_1"], rec_cfg["sample_rate_2"]}

        if not speaker_id or not text_content or round_index <= 0 or sample_rate <= 0:
            return jsonify({"ok": False, "message": "参数不完整。"}), 400
        if sample_rate not in expected_rates:
            return jsonify({"ok": False, "message": "采样率不在配置范围内。"}), 400
        if upload is None:
            return jsonify({"ok": False, "message": "缺少音频文件。"}), 400

        rec_session = RecordSession.query.filter_by(speaker_id=speaker_id).first()
        if not rec_session:
            return jsonify({"ok": False, "message": "录制会话不存在，请重新填写 ID。"}), 400

        duplicate = AudioRecord.query.filter_by(
            speaker_id=speaker_id,
            text_content=text_content,
            round_index=round_index,
            sample_rate=sample_rate,
        ).first()

        safe_text = sanitize_for_filename(text_content)
        filename = f"{speaker_id}-{safe_text}-{round_index}-{sample_rate}.wav"
        audio_bytes = upload.read()
        if not audio_bytes:
            return jsonify({"ok": False, "message": "空音频文件。"}), 400
        duration_seconds = get_wav_duration_seconds(audio_bytes)

        if duplicate:
            duplicate.user_id = session["user_id"]
            duplicate.session_id = rec_session.id
            duplicate.bit_depth = rec_cfg["bit_depth"]
            duplicate.channels = rec_cfg["channels"]
            duplicate.filename = filename
            duplicate.audio_data = audio_bytes
            duplicate.duration_seconds = duration_seconds
            duplicate.created_at = datetime.utcnow()
        else:
            rec = AudioRecord(
                user_id=session["user_id"],
                session_id=rec_session.id,
                speaker_id=speaker_id,
                text_content=text_content,
                round_index=round_index,
                sample_rate=sample_rate,
                bit_depth=rec_cfg["bit_depth"],
                channels=rec_cfg["channels"],
                filename=filename,
                audio_data=audio_bytes,
                duration_seconds=duration_seconds,
            )
            db.session.add(rec)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            return jsonify({"ok": False, "message": "保存失败，可能是重复数据。"}), 409
        except SQLAlchemyError as exc:
            db.session.rollback()
            return jsonify({"ok": False, "message": f"保存失败：{str(exc)}"}), 500

        action = "覆盖保存成功。" if duplicate else "保存成功。"
        return jsonify({"ok": True, "message": action, "filename": filename})

    @app.route("/recordings")
    @login_required
    def recordings():
        rec_cfg = app.config["RECORDING_CFG"]
        groups, incomplete_groups, expected_total = get_session_groups(rec_cfg)
        all_speaker_ids = sorted([g["speaker_id"] for g in groups])
        download_rows = build_download_rows(rec_cfg)

        records = AudioRecord.query.order_by(AudioRecord.created_at.desc()).all()
        record_user_ids = {r.user_id for r in records}
        record_users = User.query.filter(User.id.in_(record_user_ids)).all() if record_user_ids else []
        record_user_map = {u.id: u.username for u in record_users}

        return render_template(
            "recordings.html",
            groups=groups,
            download_rows=download_rows,
            all_speaker_ids=all_speaker_ids,
            incomplete_groups=incomplete_groups,
            records=records,
            expected_total=expected_total,
            record_user_map=record_user_map,
        )

    @app.post("/download")
    @login_required
    def download():
        rec_cfg = app.config["RECORDING_CFG"]
        rounds_by_text = rec_cfg["rounds"]
        rounds_map = {}
        for idx, txt in enumerate(rec_cfg["texts"]):
            rounds_map[txt] = rounds_by_text[idx] if idx < len(rounds_by_text) else 1
        selected_units = sorted(set(request.form.getlist("record_units")))
        if not selected_units:
            flash("请至少选择一条 ID+文本。", "error")
            return redirect(url_for("recordings"))

        pairs = []
        for unit in selected_units:
            if "|||" not in unit:
                continue
            sid, txt = unit.split("|||", 1)
            sid = sid.strip()
            txt = txt.strip()
            if sid and txt:
                pairs.append((sid, txt))
        if not pairs:
            flash("选择项格式无效。", "error")
            return redirect(url_for("recordings"))

        filter_conds = [and_(AudioRecord.speaker_id == sid, AudioRecord.text_content == txt) for sid, txt in pairs]

        rows = (
            AudioRecord.query.filter(or_(*filter_conds))
            .order_by(AudioRecord.text_content.asc(), AudioRecord.speaker_id.asc(), AudioRecord.filename.asc())
            .all()
        )
        if not rows:
            flash("未找到对应录音数据。", "error")
            return redirect(url_for("recordings"))

        user_ids = {r.user_id for r in rows}
        user_rows = User.query.filter(User.id.in_(user_ids)).all() if user_ids else []
        user_map = {u.id: u.username for u in user_rows}

        audio_meta_csv = StringIO()
        writer = csv.writer(audio_meta_csv)
        writer.writerow(
            [
                "speaker_id",
                "text",
                "round_index",
                "sample_rate",
                "bit_depth",
                "channels",
                "filename",
                "login_user",
                "duration_seconds",
                "created_at",
                "bytes",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    row.speaker_id,
                    row.text_content,
                    row.round_index,
                    row.sample_rate,
                    row.bit_depth,
                    row.channels,
                    row.filename,
                    user_map.get(row.user_id, f"user-{row.user_id}"),
                    f"{row.duration_seconds:.3f}",
                    row.created_at.isoformat(),
                    len(row.audio_data),
                ]
            )

        summary_csv = StringIO()
        summary_writer = csv.writer(summary_csv)
        summary_writer.writerow(["speaker_id", "text", "saved_total", "expected_total", "status"])
        unit_saved_count = {}
        for row in rows:
            key = (row.speaker_id, row.text_content)
            unit_saved_count[key] = unit_saved_count.get(key, 0) + 1
        for sid, txt in pairs:
            total = unit_saved_count.get((sid, txt), 0)
            expected_per_text = rounds_map.get(txt, 1) * 2
            if total >= expected_per_text:
                status = "已完成"
            elif total > 0:
                status = "未完成"
            else:
                status = "未录制"
            summary_writer.writerow([sid, txt, total, expected_per_text, status])

        buf = BytesIO()
        with ZipFile(buf, "w", compression=ZIP_DEFLATED) as zf:
            for row in rows:
                safe_text = sanitize_for_filename(row.text_content)
                zf.writestr(f"{row.speaker_id}/{safe_text}/{row.filename}", row.audio_data)
            zf.writestr("_metadata/audio_metadata.csv", audio_meta_csv.getvalue())
            zf.writestr("_metadata/unit_summary.csv", summary_csv.getvalue())
        buf.seek(0)

        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        return send_file(
            buf,
            mimetype="application/zip",
            as_attachment=True,
            download_name=f"audio-recordings-{ts}.zip",
        )

    @app.get("/api/audio/<int:record_id>")
    @login_required
    def audio_preview(record_id: int):
        row = AudioRecord.query.get(record_id)
        if not row:
            return jsonify({"ok": False, "message": "录音不存在。"}), 404
        return send_file(
            BytesIO(row.audio_data),
            mimetype="audio/wav",
            as_attachment=False,
            download_name=row.filename,
        )

    @app.get("/api/speaker-recordings")
    @login_required
    def speaker_recordings():
        speaker_id = request.args.get("speaker_id", "").strip()
        if not speaker_id:
            return jsonify({"ok": False, "message": "ID 不能为空。"}), 400
        rows = (
            AudioRecord.query.filter_by(speaker_id=speaker_id)
            .order_by(AudioRecord.text_content.asc(), AudioRecord.round_index.asc(), AudioRecord.sample_rate.asc())
            .all()
        )
        return jsonify(
            {
                "ok": True,
                "records": [
                    {
                        "id": r.id,
                        "speaker_id": r.speaker_id,
                        "text_content": r.text_content,
                        "round_index": r.round_index,
                        "sample_rate": r.sample_rate,
                        "filename": r.filename,
                        "duration_seconds": r.duration_seconds,
                        "created_at": r.created_at.isoformat(),
                        "audio_url": url_for("audio_preview", record_id=r.id),
                    }
                    for r in rows
                ],
            }
        )

    @app.post("/api/delete-record")
    @login_required
    def delete_record():
        payload = request.get_json(silent=True) or {}
        record_id = payload.get("record_id")
        if not record_id:
            return jsonify({"ok": False, "message": "record_id 不能为空。"}), 400
        row = AudioRecord.query.get(record_id)
        if not row:
            return jsonify({"ok": False, "message": "录音不存在。"}), 404
        speaker_id = row.speaker_id
        db.session.delete(row)
        db.session.commit()
        _, incomplete_groups, _ = get_session_groups(app.config["RECORDING_CFG"])
        return jsonify(
            {
                "ok": True,
                "message": "删除成功。",
                "speaker_id": speaker_id,
                "incomplete_groups": incomplete_groups,
            }
        )

    @app.post("/api/delete-records")
    @login_required
    def delete_records():
        payload = request.get_json(silent=True) or {}
        record_ids = payload.get("record_ids") or []
        if not isinstance(record_ids, list) or not record_ids:
            return jsonify({"ok": False, "message": "record_ids 不能为空。"}), 400
        valid_ids = []
        for rid in record_ids:
            try:
                valid_ids.append(int(rid))
            except (TypeError, ValueError):
                continue
        if not valid_ids:
            return jsonify({"ok": False, "message": "record_ids 格式错误。"}), 400

        rows = AudioRecord.query.filter(AudioRecord.id.in_(valid_ids)).all()
        if not rows:
            return jsonify({"ok": False, "message": "未找到可删除的录音。"}), 404
        for row in rows:
            db.session.delete(row)
        db.session.commit()
        _, incomplete_groups, _ = get_session_groups(app.config["RECORDING_CFG"])
        return jsonify(
            {
                "ok": True,
                "message": f"已删除 {len(rows)} 条录音。",
                "incomplete_groups": incomplete_groups,
            }
        )

    @app.post("/download-records")
    @login_required
    def download_records():
        record_ids = request.form.getlist("record_ids")
        if not record_ids:
            flash("请至少选择一条录音。", "error")
            return redirect(url_for("recordings"))
        valid_ids = []
        for rid in record_ids:
            try:
                valid_ids.append(int(rid))
            except (TypeError, ValueError):
                continue
        if not valid_ids:
            flash("录音ID格式错误。", "error")
            return redirect(url_for("recordings"))

        rows = (
            AudioRecord.query.filter(AudioRecord.id.in_(valid_ids))
            .order_by(AudioRecord.speaker_id.asc(), AudioRecord.text_content.asc(), AudioRecord.filename.asc())
            .all()
        )
        if not rows:
            flash("未找到对应录音数据。", "error")
            return redirect(url_for("recordings"))

        user_ids = {r.user_id for r in rows}
        user_rows = User.query.filter(User.id.in_(user_ids)).all() if user_ids else []
        user_map = {u.id: u.username for u in user_rows}

        audio_meta_csv = StringIO()
        writer = csv.writer(audio_meta_csv)
        writer.writerow(
            [
                "record_id",
                "speaker_id",
                "text",
                "round_index",
                "sample_rate",
                "bit_depth",
                "channels",
                "filename",
                "login_user",
                "duration_seconds",
                "created_at",
                "bytes",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    row.id,
                    row.speaker_id,
                    row.text_content,
                    row.round_index,
                    row.sample_rate,
                    row.bit_depth,
                    row.channels,
                    row.filename,
                    user_map.get(row.user_id, f"user-{row.user_id}"),
                    f"{row.duration_seconds:.3f}",
                    row.created_at.isoformat(),
                    len(row.audio_data),
                ]
            )

        buf = BytesIO()
        with ZipFile(buf, "w", compression=ZIP_DEFLATED) as zf:
            for row in rows:
                safe_text = sanitize_for_filename(row.text_content)
                zf.writestr(f"{row.speaker_id}/{safe_text}/{row.filename}", row.audio_data)
            zf.writestr("_metadata/audio_metadata.csv", audio_meta_csv.getvalue())
        buf.seek(0)

        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        return send_file(
            buf,
            mimetype="application/zip",
            as_attachment=True,
            download_name=f"audio-records-selected-{ts}.zip",
        )

    with app.app_context():
        db.create_all()
        try:
            db.session.execute(
                text("ALTER TABLE audio_records MODIFY COLUMN audio_data LONGBLOB NOT NULL")
            )
            inspector = inspect(db.engine)
            cols = {c["name"] for c in inspector.get_columns("audio_records")}
            if "duration_seconds" not in cols:
                db.session.execute(
                    text(
                        "ALTER TABLE audio_records "
                        "ADD COLUMN duration_seconds FLOAT NOT NULL DEFAULT 0"
                    )
                )
            db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()

        # 回填历史数据时长（仅 duration_seconds 为 0 的记录）。
        try:
            needs_backfill = AudioRecord.query.filter(AudioRecord.duration_seconds <= 0).all()
            changed = False
            for rec in needs_backfill:
                dur = get_wav_duration_seconds(rec.audio_data or b"")
                if dur > 0:
                    rec.duration_seconds = dur
                    changed = True
            if changed:
                db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()

    return app


app = create_app()


if __name__ == "__main__":
    conf = load_yaml_config()
    app.run(
        host=conf["app"]["host"],
        port=conf["app"]["port"],
        debug=conf["app"]["debug"],
    )
