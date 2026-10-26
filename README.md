# 🎙️ ywsj-audio-to-subtitle

免费的音频/视频转字幕工具，上传文件即可在线生成字幕。支持必剪（B站）免费引擎 + OpenAI/Groq/硅基流动等 Whisper 兼容接口，支持视频自动提取音轨、多语言识别、在线编辑、2FA 两步验证。

## ✨ 功能特性

- 🎵 **音视频支持** — MP3/WAV/M4A 等音频，MP4/MKV/MOV 等视频（自动提取音轨），最大 200MB
- 🔍 **多引擎识别** — 必剪（B站·免费·中英文）/ Groq（免费·多语言）/ OpenAI / 硅基流动，可自定义多个接口
- 📊 **实时进度** — 上传进度条（百分比+已传大小）+ 识别进度（5阶段日志）
- ✏️ **在线编辑** — 识别完成后可逐条编辑字幕文本、调整时间码、增删行
- 💾 **多格式下载** — 支持 SRT / VTT / TXT 格式下载
- 📡 **多接口管理** — 支持添加多个 API 接口，自定义备注名，一键测试连通性
- 🛡️ **2FA 两步验证** — TOTP 支持 Google Authenticator，登录双重保护
- 🔐 **安全认证** — 密码 scrypt 哈希，防暴力破解，首次部署设置密码
- 🎨 暗色 / 亮色主题切换
- 🐳 Docker 部署，amd64 + arm64 双架构

## 🚀 快速部署

### Docker

```bash
docker run -d \
  --name audio-to-subtitle \
  -p 5210:5200 \
  -v ./data:/data \
  --restart always \
  ywsj/audio-to-subtitle:latest
```

### Docker Compose

```yaml
services:
  audio-to-subtitle:
    image: ywsj/audio-to-subtitle:latest
    container_name: audio-to-subtitle
    ports:
      - "5210:5200"
    volumes:
      - ./data:/data
    restart: always
```

```bash
docker compose up -d
```

部署后访问 `http://你的IP:5210`，首次访问设置管理员账号密码。

## 📖 使用方法

1. **上传文件** — 拖拽或点击上传音频/视频文件（建议优先上传音频，速度更快）
2. **选择引擎** — 必剪（免费）或自定义 API 接口
3. **识别** — 实时查看上传进度和识别日志
4. **在线编辑** — 点击字幕文本即可编辑，修正错别字、调整时间
5. **下载** — 选择 SRT / VTT / TXT 格式下载

## 🔍 识别引擎

### 内置免费引擎

| 引擎 | 语言 | 配置 | 说明 |
|------|------|:---:|------|
| 必剪 (B站) | 中文、英文 | 无需配置 | 免费，调用B站官方API，开箱即用 |

### 自定义 API 接口（设置页面配置）

| 类型 | 语言 | 免费额度 | 申请地址 |
|------|------|---------|---------|
| 🟠 Groq | 90+语言 | 每天约4小时音频 | [console.groq.com/keys](https://console.groq.com/keys) |
| 🟢 硅基流动 | 中/英/日/韩/粤 | 注册送额度 | [cloud.siliconflow.cn](https://cloud.siliconflow.cn) |
| 🔵 OpenAI | 90+语言 | 付费 $0.006/分钟 | [platform.openai.com](https://platform.openai.com/api-keys) |

> 💡 **推荐 Groq**：免费额度大、速度极快、返回精确逐句时间戳（断句最好）。Google 账号注册即可。
>
> ⚠️ 必剪引擎返回纯文本无逐句时间戳，断句为智能估算；Whisper 系（Groq/OpenAI）返回精确时间戳。

### 接口管理

- 支持添加**多个同类接口**（如 2 个硅基流动账号），自定义备注名区分
- 每个接口可**一键测试连通性**（发送静音音频验证）
- 引擎下拉框显示自定义接口名称

## 🛡️ 安全功能

### 2FA 两步验证

设置 → 账户安全 → 启用两步验证：
1. 用 Google Authenticator 扫描二维码
2. 输入 APP 显示的 6 位验证码确认
3. 之后每次登录需输入密码 + 验证码

### 其他安全措施

- 密码 scrypt 加盐哈希存储
- 防暴力破解（同 IP 失败 5 次锁定 15 分钟）
- 2FA 验证码 5 次错误锁定，3 分钟超时
- API Key 脱敏显示，不进 Git/镜像

## 🛠️ 技术栈

- **后端** — Python / Flask / Gunicorn
- **前端** — 原生 HTML + CSS + JavaScript
- **音频处理** — pydub + ffmpeg（视频提取音轨）
- **2FA** — pyotp + qrcode（本地生成）
- **数据库** — SQLite（用户认证 + 识别历史 + 接口配置）
- **部署** — Docker（多架构 amd64 + arm64）
- **CI/CD** — GitHub Actions 自动构建推送 Docker Hub + GitHub Release

## 📂 项目结构

```
ywsj-audio-to-subtitle/
├── app.py              # Flask 主应用（路由+认证+2FA+接口管理）
├── asr_engine.py       # 语音识别引擎（必剪+OpenAI兼容）
├── templates/
│   ├── index.html      # 主页面（上传+识别+编辑+设置）
│   ├── setup.html      # 首次初始化设置密码
│   └── login.html      # 登录页（含2FA验证阶段）
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── .github/workflows/
    └── docker-publish.yml
```

## 🙏 致谢

- [VideoCaptioner](https://github.com/WEIFENG2333/VideoCaptioner) — 必剪/剪映 ASR 接口实现参考
- [Flask](https://flask.palletsprojects.com/) — Web 框架
- [pydub](https://github.com/jiaaro/pydub) — 音频处理库
- [pyotp](https://github.com/pyauth/pyotp) — TOTP 两步验证

## ☕ 请作者喝杯咖啡

如果这个项目对你有帮助，欢迎请作者喝杯咖啡 ☕️

![打赏码](https://raw.githubusercontent.com/yyzq-cf/ywsj-audio-to-subtitle/main/assets/donation.jpg)

## 📄 License

MIT
