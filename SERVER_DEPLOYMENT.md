# TradingAgents Web 服务器部署 README

这份文档用于把 TradingAgents 可视化分析工作台部署到一台服务器上，例如海外云服务器，然后由国内用户通过浏览器访问。

推荐部署形态：

- Web 服务运行在服务器内网端口 `8000`
- Nginx、Caddy 或云负载均衡负责 HTTPS 和公网访问
- API Key、管理员密码和数据库路径只保存在服务器 `.env` 或服务环境变量里
- SQLite 数据库、报告、缓存目录放在持久化磁盘或 Docker volume 中

## 1. 前置条件

服务器建议：

- Linux x86_64，Ubuntu 22.04/24.04 或同类发行版
- 2 CPU / 4 GB 内存起步，长报告或多人使用建议 4 CPU / 8 GB+
- 已安装 Docker 和 Docker Compose Plugin
- 一个域名，例如 `agents.example.com`
- 防火墙只开放 `80/443`，不要直接暴露 `8000`

代码来源建议使用你的 fork：

```bash
git clone https://github.com/moruowait/TradingAgents.git
cd TradingAgents
git checkout codex/deploy-web-ui
```

## 2. 配置 `.env`

复制示例配置：

```bash
cp .env.example .env
chmod 600 .env
```

至少配置下面这些项：

```bash
# 管理员初始密码。首次生产部署不要继续使用默认 123456。
TRADINGAGENTS_WEB_ADMIN_PASSWORD=change-this-password

# 浏览器里展示/回调用的公开地址，建议填写 HTTPS 域名。
TRADINGAGENTS_WEB_PUBLIC_URL=https://agents.example.com

# LLM 基础配置，按实际供应商填写。
TRADINGAGENTS_LLM_PROVIDER=openai_compatible
TRADINGAGENTS_DEEP_THINK_LLM=gpt-5.5
TRADINGAGENTS_QUICK_THINK_LLM=gpt-5.5
TRADINGAGENTS_LLM_BACKEND_URL=https://your-llm-gateway.example.com/v1
TRADINGAGENTS_LLM_TIMEOUT=180
TRADINGAGENTS_LLM_MAX_RETRIES=6

# LLM 密钥。选择你实际使用的供应商即可。
OPENAI_COMPATIBLE_API_KEY=your-key

# 业务数据密钥，按需配置。
FRED_API_KEY=your-fred-key
ALPHA_VANTAGE_API_KEY=your-alpha-vantage-key
```

配置分类说明：

- LLM 基础配置：`TRADINGAGENTS_LLM_PROVIDER`、模型名、`TRADINGAGENTS_LLM_BACKEND_URL`、timeout、retry、temperature 等。
- LLM 密钥：`OPENAI_API_KEY`、`OPENAI_COMPATIBLE_API_KEY`、`DASHSCOPE_API_KEY` 等。
- 业务运行配置：数据源、结果目录、缓存目录、新闻抓取限制等。
- 用户行为配置：用户在页面提交的 ticker、日期、分析师选择、语言、讨论轮数等，不应写入系统 `.env`。

## 3. Docker 部署

构建并启动 Web 服务：

```bash
docker compose --profile web up -d --build tradingagents-web
```

查看日志：

```bash
docker compose --profile web logs -f tradingagents-web
```

确认容器运行：

```bash
docker compose --profile web ps
```

默认 Compose 会把应用数据挂载到 Docker volume：

```yaml
volumes:
  - tradingagents_data:/home/appuser/.tradingagents
```

SQLite 数据库默认位置：

```text
/home/appuser/.tradingagents/logs/web/state.sqlite3
```

报告、任务历史和缓存也会在 `/home/appuser/.tradingagents` 下持久化。

## 4. Nginx 反向代理

不要让用户直接访问 `http://server-ip:8000`。建议用 Nginx 对外提供 HTTPS：

```nginx
server {
    listen 80;
    server_name agents.example.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name agents.example.com;

    ssl_certificate /etc/letsencrypt/live/agents.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/agents.example.com/privkey.pem;

    client_max_body_size 20m;
    proxy_read_timeout 600s;
    proxy_send_timeout 600s;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

重载 Nginx：

```bash
sudo nginx -t
sudo systemctl reload nginx
```

如果使用 Caddy，可以用更短的配置：

```caddyfile
agents.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

## 5. 首次登录和用户管理

访问：

```text
https://agents.example.com
```

默认管理员账号：

```text
账号：admin
密码：TRADINGAGENTS_WEB_ADMIN_PASSWORD 的值
```

如果没有配置 `TRADINGAGENTS_WEB_ADMIN_PASSWORD`，服务会使用默认密码 `123456`。生产环境必须改掉。

管理员登录后：

- 打开 `用户管理`
- 新增普通用户
- 删除不需要的普通用户
- 每个用户只能看到自己的分析任务、报告历史、报告预览和下载

## 6. 数据持久化和备份

Docker volume 中的数据不会跟随 git 提交，也不会写入代码仓库。

建议定期备份：

```bash
docker compose --profile web exec tradingagents-web sh -lc \
  'mkdir -p /home/appuser/.tradingagents/backups && cp /home/appuser/.tradingagents/logs/web/state.sqlite3 /home/appuser/.tradingagents/backups/state-$(date +%Y%m%d-%H%M%S).sqlite3'
```

也可以直接备份整个 Docker volume：

```bash
docker run --rm \
  -v tradingagents-dev_tradingagents_data:/data \
  -v "$PWD/backups:/backup" \
  alpine sh -lc 'tar czf /backup/tradingagents-data-$(date +%Y%m%d-%H%M%S).tgz -C /data .'
```

注意：volume 名称可能随目录名变化。用下面命令确认：

```bash
docker volume ls | grep tradingagents
```

## 7. 升级发布

拉取最新代码并重建：

```bash
git fetch origin
git checkout codex/deploy-web-ui
git pull --ff-only origin codex/deploy-web-ui
docker compose --profile web up -d --build tradingagents-web
```

升级前建议先备份 SQLite 数据库和报告目录。

升级后查看日志：

```bash
docker compose --profile web logs --tail=200 tradingagents-web
```

## 8. 安全建议

生产环境至少做到：

- 修改默认管理员密码，不使用 `123456`
- `.env` 权限设置为 `600`
- 只开放公网 `80/443`，不要开放 `8000`
- 使用 HTTPS
- API Key 只放服务器环境变量或 `.env`，不要放前端代码
- 定期备份 SQLite 和报告目录
- 给服务器加安全组或 IP 白名单，团队内部使用时优先走 VPN、Zero Trust 或云网关

如果部署在海外服务器给国内访问，建议：

- 选择国内访问质量稳定的云厂商和区域
- LLM 网关也放在同一区域或网络质量较好的区域
- 在 Nginx 上把 `proxy_read_timeout` 设置得长一些，避免长分析任务请求被提前断开
- 业务请求通过页面提交后由服务器执行，用户浏览器不需要直接访问 LLM 或数据供应商 API

## 9. 常见问题

### 页面显示未连接

检查服务是否运行：

```bash
docker compose --profile web ps
docker compose --profile web logs --tail=100 tradingagents-web
```

检查反向代理是否指向 `127.0.0.1:8000`，并确认容器端口已映射：

```bash
curl -I http://127.0.0.1:8000
```

### 管理员无法新增用户

确认当前登录账号是 `admin`，并查看日志是否有数据库权限错误：

```bash
docker compose --profile web logs --tail=200 tradingagents-web
```

如果自定义了 `TRADINGAGENTS_WEB_DB_PATH`，确认该目录对容器内 `appuser` 可写。

### 数据刷新后丢失

确认 `/home/appuser/.tradingagents` 使用了 Docker volume 或宿主机持久化目录。不要把数据库放在容器临时目录里。

### 分析任务连接 LLM 失败

检查系统设置页中的：

- `LLM 基础配置`
- `LLM 密钥状态`
- `业务数据密钥状态`

同时在服务器上检查 `.env` 是否配置了正确的 provider、backend URL 和 API Key。

### 需要重置管理员密码

修改 `.env`：

```bash
TRADINGAGENTS_WEB_ADMIN_PASSWORD=new-password
```

然后重启服务：

```bash
docker compose --profile web restart tradingagents-web
```

如果数据库中已存在管理员账号，服务初始化逻辑不会删除已有任务和报告。必要时可以在数据库层面重置密码，但生产环境操作前必须先备份 SQLite。

