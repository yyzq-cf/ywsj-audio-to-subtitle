# 🎙️ ywsj-audio-to-subtitle

免费的音频转字幕工具，上传 MP3/WAV/M4A 等音频文件即可在线生成字幕。基于必剪（B站）和剪映（字节）的免费云端语音识别 API，无需任何 API Key，开箱即用。

## ✨ 功能特性

- 🎵 **多格式支持** — MP3 / WAV / M4A / FLAC / OGG / AAC 等，最大 200MB
- 🔍 **免费语音识别** — 内置必剪（B站）和剪映（字节）两个引擎，无需配置
- ✏️ **在线编辑** — 识别完成后可逐条编辑字幕文本，修正错别字
- ➕ **增删行** — 可添加新字幕行或删除不需要的行
- 💾 **多格式下载** — 支持 SRT / VTT / TXT 格式下载
- 📊 **实时进度** — 上传、识别进度条 + 实时日志输出
- 🔐 **登录认证** — 首次部署设置密码，密码 scrypt 加盐哈希存储
- 🛡️ **防暴力破解** — 同 IP 失败 5 次锁定 15 分钟
- 🐳 **Docker 部署** — 支持 amd64 + arm64 双架构，GitHub Actions 自动构建发布
- 🎨 暗色 / 亮色主题切换

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

1. **上传音频文件** — 拖拽或点击上传音频文件
2. **选择引擎** — 必剪（B站）或剪映（字节），均为免费
3. **识别** — 点击开始识别，实时查看进度和日志
4. **在线编辑** — 点击字幕文本即可编辑，修正错别字
5. **下载** — 选择 SRT / VTT / TXT 格式下载

### 引擎说明

| 引擎 | 原理 | 免费 | 说明 |
|------|------|:---:|------|
| 必剪 (B站) | 调用 B站必剪云端 ASR API | ✅ | 无需配置，有频率限制 |
| 剪映 (字节) | 调用字节剪映云端 ASR API | ✅ | 无需配置，有频率限制 |

> 两个引擎均为免费云端识别，接口可能随时变动。如需更稳定的方案，建议使用 Whisper 本地模型。

## 🛠️ 技术栈

- **后端** — Python / Flask / Gunicorn
- **前端** — 原生 HTML + CSS + JavaScript
- **音频处理** — pydub + ffmpeg
- **数据库** — SQLite（用户认证 + 识别历史）
- **部署** — Docker（多架构 amd64 + arm64）
- **CI/CD** — GitHub Actions 自动构建推送 Docker Hub + 创建 GitHub Release

## 📂 项目结构

```
ywsj-audio-to-subtitle/
├── app.py              # Flask 主应用
├── asr_engine.py       # 语音识别引擎（必剪 + 剪映）
├── templates/
│   ├── index.html      # 主页面（上传 + 识别 + 编辑）
│   ├── setup.html      # 初始化设置密码
│   └── login.html      # 登录页
├── Dockerfile
├── docker-compose.yml
└── .github/workflows/
    └── docker-publish.yml
```

## 🙏 致谢

- [VideoCaptioner](https://github.com/WEIFENG2333/VideoCaptioner) — 必剪/剪映 ASR 接口实现参考
- [Flask](https://flask.palletsprojects.com/) — Web 框架
- [pydub](https://github.com/jiaaro/pydub) — 音频处理库

## ☕ 请作者喝杯咖啡

如果这个项目对你有帮助，欢迎请作者喝杯咖啡 ☕️

![打赏码](assets/donation.jpg)

## 📄 License

MIT
