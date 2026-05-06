# Flask 音频采集系统使用说明

本项目是一个基于 Flask 的轻量级音频采集与管理系统，支持：

- 用户注册 / 登录
- 按文本与轮次进行双采样率录音（如 32000 / 16000）
- 音频保存到 MySQL（含元数据）
- 录音进度管理（未完成 ID 提示与继续录制）
- 结果查看、筛选、预览、删除与 ZIP 下载
- 页面主题切换（白色 / 灰色 / 深蓝色 / 护眼色）

---

## 1. 环境与依赖

### 1.1 系统要求

- Linux / macOS / Windows（推荐 Linux）
- Python 3.10+
- MySQL 8.x（或兼容版本）

### 1.2 Python 依赖

依赖列表位于 `requirements.txt`：

- `Flask`
- `Flask-SQLAlchemy`
- `PyMySQL`
- `PyYAML`

安装方式：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

---

## 2. 数据库安装与设置

你可以二选一：**本机 MySQL** 或 **Docker MySQL**。

### 2.1 方式A：本机安装 MySQL（apt）

```bash
sudo apt update
sudo apt install -y mysql-server
sudo systemctl enable --now mysql
```

创建数据库与业务用户：

```bash
sudo mysql -e "CREATE DATABASE IF NOT EXISTS audio_set DEFAULT CHARACTER SET utf8mb4;"
sudo mysql -e "CREATE USER IF NOT EXISTS 'audio_user'@'%' IDENTIFIED BY 'audio_pass';"
sudo mysql -e "GRANT ALL PRIVILEGES ON audio_set.* TO 'audio_user'@'%';"
sudo mysql -e "FLUSH PRIVILEGES;"
```

### 2.2 方式B：Docker 安装 MySQL（推荐，避免环境冲突）

```bash
sudo docker run -d \
  --name audio-set-mysql \
  -p 3307:3306 \
  -e MYSQL_ROOT_PASSWORD=root \
  -e MYSQL_DATABASE=audio_set \
  -e MYSQL_USER=audio_user \
  -e MYSQL_PASSWORD=audio_pass \
  --restart unless-stopped \
  mysql:8.0
```

> 如果本机 `3306` 已被占用，建议像上面一样映射到 `3307`。

---

## 3. 配置文件说明（`config.yaml`）

项目使用 `config.yaml` 作为运行配置。示例：

```yaml
app:
  secret_key: "please-change-this-secret-key"
  host: "0.0.0.0"
  port: 8002
  debug: true

database:
  # 可直接使用 uri（推荐）
  uri: "mysql+pymysql://audio_user:audio_pass@127.0.0.1:3307/audio_set?charset=utf8mb4"
  # 如果不用 uri，也可填写以下拆分字段
  host: "127.0.0.1"
  port: 3307
  name: "audio_set"
  user: "audio_user"
  password: "audio_pass"
  charset: "utf8mb4"

recording:
  texts: ["123", "abc"]
  rounds: 10
  sample_rate_1: 32000
  sample_rate_2: 16000
  bit_depth: 16
  channels: 1
```

### 3.1 关键配置项

- `app.secret_key`：Flask Session 密钥，生产环境请改成强随机值
- `app.host` / `app.port`：服务监听地址与端口
- `database.uri`：数据库连接串（优先级高于拆分字段）
- `recording.texts`：录音文本列表
- `recording.rounds`：每个文本录制轮数
- `recording.sample_rate_1 / sample_rate_2`：双采样率
- `recording.bit_depth` / `channels`：位深与通道数

---

## 4. 启动项目

### 4.1 初始化数据库表

首次启动前建议执行：

```bash
flask --app app init-db
```

> 即使不手动执行，程序启动时也会自动建表/补字段。

### 4.2 启动服务

```bash
python3 app.py
```

启动后访问（按你的配置）：

- 本机：`http://127.0.0.1:8002`
- 局域网：`http://<服务器IP>:8002`

---

## 5. 停止方式

### 5.1 前台运行时

在运行终端按：

```bash
Ctrl + C
```

### 5.2 后台运行时（自行 nohup / screen / tmux）

查进程并结束：

```bash
ps -ef | grep "python3 app.py"
kill <PID>
```

### 5.3 停止 Docker MySQL（如果你用的是容器）

```bash
sudo docker stop audio-set-mysql
```

重新启动：

```bash
sudo docker start audio-set-mysql
```

---

## 6. 常见问题

### 6.1 `Unexpected token '<' ... is not valid JSON`

通常表示某个 `/api/*` 请求后端报错，返回了 HTML 错误页。  
请先查看后端日志，定位真实异常（数据库连接、字段缺失、权限等）。

### 6.2 无法连接 MySQL

- 检查 `config.yaml` 中 `database.uri` 是否正确
- 检查 MySQL 是否启动
- 检查端口是否冲突（3306/3307）
- 检查用户名密码与授权范围

### 6.3 录音页无法采集麦克风

- 浏览器需允许麦克风权限
- 推荐使用最新 Chrome/Edge
- HTTP 场景下某些浏览器策略更严格，建议同机访问或使用 HTTPS

---

## 7. 目录结构（核心）

- `app.py`：后端主程序（路由、模型、下载、元数据）
- `config.yaml`：项目配置
- `templates/`：页面模板
- `static/js/record.js`：录音页核心前端逻辑
- `requirements.txt`：Python 依赖
