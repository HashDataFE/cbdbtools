# CBDBTools 产品设计文档

> 版本: v2.0 | 日期: 2026-04-14 | 状态: Release

---

## 1. 产品概述

### 1.1 产品定位

CBDBTools 是一套面向 MPP 数据库集群的**自动化部署工具**，支持通过命令行(CLI)或 Web UI 两种方式，在 CentOS/RHEL 7-9、Rocky Linux 8-9、Oracle Linux 8 以及 Ubuntu 20.04-24.04 上一键完成数据库集群的环境初始化和集群创建。

### 1.2 目标用户

- 数据库管理员(DBA)
- 运维工程师
- 需要快速搭建测试/开发环境的开发人员

### 1.3 支持的数据库

| 数据库 | 版本 | 安装路径 |
|--------|------|----------|
| Cloudberry DB | 1.x, 2.x | `/usr/local/cloudberry-db` |
| Greenplum | 5.x, 6.x, 7.x | `/usr/local/greenplum-db` |
| HashData Lightning | 1.x, 2.x | `/usr/local/hashdata-lightning` 或 `/usr/local/cloudberry-db` |
| SynxDB | 1.x, 2.x, 4.x | `/usr/local/synxdb` 或 `/usr/local/synxdb4` |

### 1.4 支持的操作系统

| 系列 | 版本 | 包格式 | 包管理器 |
|------|------|--------|----------|
| CentOS / RHEL | 7, 8, 9 | RPM | yum / dnf |
| Rocky Linux | 8, 9 | RPM | yum / dnf |
| Oracle Linux | 8 | RPM | yum / dnf |
| Ubuntu | 20.04, 22.04, 24.04 | DEB | apt |

> 工具需根据操作系统自动检测包格式，使用对应的包管理器安装数据库软件和依赖。

---

## 2. 系统架构

### 2.1 整体架构图

**所有操作均在 Coordinator 节点上执行。** Web UI 是运行在 Coordinator 上的本地 Web 服务，为用户提供图形化的参数输入和部署管理界面，底层调用的是同一套部署脚本。

```
              Coordinator 节点
┌──────────────────────────────────────────────┐
│                                              │
│   用户入口 (二选一)                            │
│   ┌─────────────┐    ┌────────────────────┐  │
│   │  CLI 模式    │    │   Web UI 模式       │  │
│   │  sh run.sh  │    │   Flask (port 5000) │  │
│   └──────┬──────┘    └─────────┬──────────┘  │
│          │                     │              │
│          │    本地调用同一套脚本  │              │
│          └──────────┬──────────┘              │
│                     │                         │
│          +----------+----------+              │
│          |   deploycluster.sh   |  核心编排层   │
│          +----------+----------+              │
│                     │                         │
│        +------------+------------+            │
│        |                         |            │
│  +-----+------+         +-------+--------+   │
│  | init_env.sh |         | init_cluster.sh |   │
│  | (环境初始化)  |         | (集群初始化)     |   │
│  +-----+------+         +----------------+   │
│        |                                      │
└────────|──────────────────────────────────────┘
         |  多节点场景: SSH 到 Segment 节点
         |
   +-----+--------+
   |init_env_segment|  通过 multissh/multiscp 并行执行
   +---------------+
```

### 2.2 部署模式

| 模式 | 说明 | 适用场景 |
|------|------|----------|
| **单节点 (single)** | Coordinator + Segments 在同一台机器 | 开发/测试环境 |
| **多节点 (multi)** | Coordinator + 多台 Segment 主机 | 生产/准生产环境 |

### 2.3 两种使用入口

> **核心原则**: 两种模式都在 Coordinator 节点本地运行，执行同一套部署脚本。Web UI 只是 CLI 的图形化前端，不引入额外的远程调用层。

#### CLI 模式
- 在 Coordinator 上直接执行 `sh run.sh`
- 配置写在 `deploycluster_parameter.sh`
- 日志输出到 `deploy_cluster_YYYYMMDD_HHMMSS.log`
- 适合自动化脚本/CI 集成、熟悉命令行的用户

#### Web UI 模式
- 在 Coordinator 上启动 Flask 服务 (`sh start_web.sh`，监听 port 5000)
- 用户通过浏览器访问 Coordinator 的 Web 界面
- 提供图形化的参数填写、配置验证和部署管理
- 底层生成配置文件后调用同一套 Shell 脚本执行部署
- 适合偏好图形界面的用户，降低配置出错概率

---

## 3. 核心流程设计

### 3.1 CLI 部署流程

```
run.sh
 ├─ 1. 检查是否有正在运行的部署进程 (防重复)
 ├─ 2. 确定部署类型 (参数 > 配置文件)
 └─ 3. nohup 后台启动 deploycluster.sh
      │
      ├─ 4. 从安装包文件名 (RPM/DEB) 自动检测数据库类型/版本/路径
      ├─ 5. 更新 deploycluster_parameter.sh (写入检测结果)
      ├─ 6. 调用 init_env.sh (环境初始化)
      │    ├─ 配置软件源 (YUM/APT)
      │    ├─ 关闭防火墙/SELinux
      │    ├─ 配置内核参数 (sysctl)
      │    ├─ 配置资源限制 (limits.conf)
      │    ├─ 配置 SSH 守护进程
      │    ├─ 配置 systemd-logind (RemoveIPC=no)
      │    ├─ 创建管理员用户 + SSH 密钥
      │    ├─ 安装数据库软件包 (RPM/DEB)
      │    ├─ 创建数据目录
      │    └─ [多节点] 并行初始化所有 Segment 节点
      │         ├─ multiscp: 分发脚本和安装包
      │         ├─ multissh: 并行执行 init_env_segment.sh
      │         └─ 收集/分发 SSH 密钥 (免密登录)
      │
      └─ 7. 调用 init_cluster.sh (集群初始化, 可选跳过)
           ├─ 生成 gpinitsystem 配置文件
           ├─ 生成机器列表文件
           ├─ 执行 gpinitsystem
           ├─ 设置数据库管理员密码
           ├─ 配置 pg_hba.conf (远程访问)
           ├─ 配置 .bashrc 环境变量
           └─ gpstop -u 重载配置
```

### 3.2 Web UI 部署流程

> Web UI 运行在 Coordinator 节点上，用户通过浏览器访问。采用分步向导 (Wizard) 模式，每一步完成校验后才能进入下一步。

```
用户浏览器 ──(HTTP)──→ Coordinator:5000 (Flask)
 │
 ├─ Step 1: 环境配置
 │    ├─ 选择操作系统类型 (自动检测，可手动覆盖)
 │    ├─ 选择部署类型 (单节点 / 多节点)
 │    ├─ 填写安装包路径 (RPM/DEB)
 │    └─ [校验] 安装包文件是否存在、格式是否匹配 OS
 │
 ├─ Step 2: 集群配置
 │    ├─ Coordinator 主机名 / IP
 │    ├─ 管理员用户 / 密码
 │    ├─ [多节点] Segment 主机列表 (动态增删)
 │    ├─ [多节点] Segment 访问方式 (密钥/密码)
 │    ├─ 高级选项 (折叠，展开可配置):
 │    │    ├─ 数据库端口、数据目录
 │    │    ├─ Mirror / Standby 开关
 │    │    ├─ 集群名称、Segment 前缀
 │    │    └─ 软件源配置、时区
 │    └─ [校验] 主机名/IP 格式、端口范围、路径合法性
 │
 ├─ Step 3: 确认部署
 │    ├─ 展示完整的部署配置摘要 (只读):
 │    │    ├─ 基础信息: OS 类型、部署模式、安装包路径
 │    │    ├─ 集群拓扑: Coordinator + Segment 列表
 │    │    ├─ 数据库参数: 端口、数据目录、Mirror/Standby
 │    │    └─ 检测结果: 数据库类型、版本、安装路径 (预判)
 │    ├─ 异常项高亮提示 (如: 未配置 Mirror、默认密码等)
 │    ├─ [返回修改] 可回到任意步骤修改
 │    └─ [确认部署] 开始执行
 │
 └─ Step 4: 部署执行
      ├─ 顶部: 部署状态总览 (进行中 / 成功 / 失败)
      ├─ 进度指示: 当前执行阶段
      │    ├─ 环境初始化
      │    ├─ 安装数据库软件
      │    ├─ [多节点] Segment 节点初始化
      │    ├─ 集群创建
      │    └─ 配置完成
      ├─ 实时日志输出 (SSE 推送，自动滚动)
      └─ 部署完成后: 显示连接信息 (主机、端口、用户)
```

---

## 4. 模块设计

### 4.1 文件清单与职责

| 文件 | 类型 | 职责 |
|------|------|------|
| `run.sh` | Shell | CLI 入口，防重复执行，后台启动部署 |
| `deploycluster.sh` | Shell | 核心编排：数据库类型检测 + 调度 init 脚本 |
| `deploycluster_parameter.sh` | Shell | 中央配置文件，所有参数定义 |
| `init_env.sh` | Shell | Coordinator 环境初始化 |
| `init_env_segment.sh` | Shell | Segment 节点环境初始化 |
| `init_cluster.sh` | Shell | gpinitsystem 集群初始化 |
| `common.sh` | Shell | 共享函数库 (日志/系统配置/用户管理等) |
| `multissh.sh` | Shell | 并行远程命令执行工具 |
| `multiscp.sh` | Shell | 并行远程文件分发工具 |
| `segmenthosts.conf` | Conf | 主机名 ↔ IP 映射配置 |
| `mirrorlessfailover.sh` | Shell | 无 Mirror 场景下的故障转移工具 |
| `cluster_deploy_web.py` | Python | Flask Web 应用 |
| `start_web.sh` | Shell | Web 服务启动脚本 (gunicorn 单进程多线程) |
| `wsgi.py` | Python | WSGI 入口 |
| `templates/index.html` | HTML | 前端单页应用 |
| `test_web.py` | Python | Web 应用单元测试 |
| `sshpass-1.10.tar.gz` | Binary | sshpass 源码包 (离线安装用) |

### 4.2 common.sh 函数库

| 函数 | 功能 |
|------|------|
| `log_time()` | 带时间戳的日志输出 |
| `change_hostname()` | 修改主机名 (跨 OS 兼容) |
| `disable_firewall()` | 关闭 firewalld + SELinux |
| `configure_repo()` | 配置软件源 (RHEL: YUM/DNF, Ubuntu: APT) |
| `configure_sysctl()` | 内核参数调优 |
| `configure_limits()` | 资源限制配置 |
| `configure_sshd()` | SSH 守护进程优化 |
| `configure_logind()` | systemd-logind 配置 |
| `configure_timezone()` | 时区设置 |
| `install_dependencies()` | 安装依赖包 |
| `install_sshpass()` | 安装 sshpass (yum/apt 或编译) |
| `create_admin_user()` | 创建数据库管理员用户 |
| `install_db_software()` | 安装数据库软件包 (RPM/DEB，自动检测) |
| `create_data_directories()` | 创建数据目录 |

### 4.3 Web UI 路由设计

| 路由 | 方法 | 对应步骤 | 功能 | 返回 |
|------|------|----------|------|------|
| `/` | GET | - | 主页面，加载向导界面 | HTML |
| `/detect_os` | GET | Step 1 | 自动检测当前操作系统类型和版本 | JSON |
| `/validate_pkg_path` | POST | Step 1 | 验证安装包路径是否存在且格式匹配 | JSON |
| `/upload_package` | POST | Step 1 | 从浏览器上传 RPM/DEB 安装包到 `/tmp/uploads/` | JSON |
| `/save_config` | POST | Step 2 | 保存集群配置到 Session | JSON |
| `/preview_config` | GET | Step 3 | 返回完整配置摘要 (含预检测结果) | JSON |
| `/deploy` | POST | Step 4 | 生成配置文件，启动本地部署 | JSON |
| `/deployment_status` | GET | Step 4 | 查询部署状态和当前阶段 | JSON |
| `/stream_logs` | GET | Step 4 | SSE 实时日志流 | EventStream |
| `/reset` | POST | Step 4 | 重置部署状态，允许重新部署 | JSON |

### 4.4 数据库类型自动检测

`deploycluster.sh` 通过安装包文件名 (RPM 或 DEB) 模式匹配自动识别数据库类型：

| 安装包文件名匹配 | DB_TYPE | BINARY_PATH | LEGACY |
|----------------|---------|-------------|--------|
| `*cloudberry*` | Cloudberry | `/usr/local/cloudberry-db` | false |
| `*greenplum*` v5-6 | Greenplum | `/usr/local/greenplum-db` | true |
| `*greenplum*` v7+ | Greenplum | `/usr/local/greenplum-db` | false |
| `*hashdata-lightning-2*` | HashData | `/usr/local/hashdata-lightning` | false |
| `*hashdata-lightning-1*` | HashData | `/usr/local/cloudberry-db` | false |
| `*synxdb4*` | SynxDB | `/usr/local/synxdb4` | false |
| `*synxdb-2*` | SynxDB | `/usr/local/synxdb` | true |
| `*synxdb-1*` | SynxDB | `/usr/local/synxdb` | true |

> **LEGACY_VERSION** 影响 gpinitsystem 配置：`true` 时使用 `MASTER_HOSTNAME`，`false` 时使用 `COORDINATOR_HOSTNAME`。

### 4.6 操作系统检测与包管理

部署脚本在运行时自动检测操作系统类型，据此选择对应的包管理策略：

| 检测方式 | RHEL 系 | Ubuntu/Debian 系 |
|----------|---------|-------------------|
| 识别方法 | `/etc/redhat-release` 或 `ID=centos/rhel/ol/rocky` | `ID=ubuntu/debian` in `/etc/os-release` |
| 包格式 | RPM (`.rpm`) | DEB (`.deb`) |
| 包管理器 | `yum` / `dnf` | `apt` |
| 安装命令 | `rpm -ivh` / `yum localinstall` | `dpkg -i` + `apt-get install -f` |
| 软件源配置 | `/etc/yum.repos.d/` | `/etc/apt/sources.list.d/` |
| 防火墙 | `firewalld` | `ufw` |
| 依赖包名差异 | `openssh-server`, `net-tools`, `python3` 等 | `openssh-server`, `net-tools`, `python3` 等 (包名可能不同) |

> **设计要点**: `common.sh` 中的每个安装/配置函数需内部判断 OS 类型，对外接口保持统一，调用方无需关心底层差异。

---

## 5. 配置设计

### 5.1 必填参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `ADMIN_USER` | gpadmin | 数据库操作系统用户 |
| `ADMIN_USER_PASSWORD` | - | 用户密码 |
| `CLOUDBERRY_RPM` | - | 数据库安装包绝对路径 (RPM 或 DEB 文件) |
| `COORDINATOR_HOSTNAME` | - | Coordinator 主机名 |
| `COORDINATOR_IP` | - | Coordinator IP 地址 |
| `DEPLOY_TYPE` | single | 部署模式 (single/multi) |

### 5.2 可选参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `INIT_ENV_ONLY` | false | 仅初始化环境，跳过集群创建 |
| `INSTALL_DB_SOFTWARE` | true | 是否安装数据库软件包 (重新初始化时设为 false) |
| `WITH_MIRROR` | false | 启用 Mirror Segment |
| `WITH_STANDBY` | false | 启用 Standby Coordinator |
| `MANUAL_REPO` | false | 跳过软件源自动配置 |
| `TIMEZONE` | Asia/Shanghai | 目标时区 |
| `CLOUDBERRY_PKG_URL` | - | 安装包下载 URL (文件不存在时使用) |

### 5.3 集群参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `COORDINATOR_PORT` | 5432 | 数据库端口 |
| `COORDINATOR_DIRECTORY` | /data0/coordinator/gpseg-1 | Coordinator 数据目录 |
| `ARRAY_NAME` | gp | 集群名称 |
| `SEG_PREFIX` | gpseg | Segment 前缀 |
| `PORT_BASE` | 6000 | Segment 起始端口 |
| `DATA_DIRECTORY` | /data0/primary /data0/primary | 数据目录列表 (空格分隔) |

### 5.4 多节点访问参数

| 参数 | 说明 |
|------|------|
| `SEGMENT_ACCESS_METHOD` | 认证方式: `keyfile` 或 `password` |
| `SEGMENT_ACCESS_USER` | 远程登录用户名 |
| `SEGMENT_ACCESS_KEYFILE` | 私钥文件路径 |
| `SEGMENT_ACCESS_PASSWORD` | 密码 |

### 5.5 配置优先级 (Web UI)

```
内置默认值 < 已有 deploycluster_parameter.sh < Web UI 表单输入
```

---

## 6. 安全设计

### 6.1 输入验证

| 验证项 | 方法 |
|--------|------|
| IP 地址 | 格式校验 + 每段 0-255 范围检查 |
| 主机名 | 字母数字和连字符，不以连字符开头结尾 |
| 端口 | 1-65535 范围 |
| 文件路径 | 阻止 `..` 路径穿越和 null 字节 |
| 部署类型 | 白名单: 仅 `single` 或 `multi` |
| 命令注入 | `shlex.quote()` 转义用户输入 |

### 6.2 认证与凭据

- SSH 支持密码和密钥两种认证方式
- 密钥文件保存到 `/tmp`，权限 `0o600`
- 密码存储在 Flask Session 和服务端 `SAVED_CONFIG` 中，部署时写入 `deploycluster_parameter.sh` 配置文件（明文）
- 数据库管理员密码使用 `ADMIN_USER_PASSWORD` 配置参数
- 部署完成后集群内使用 SSH 密钥免密访问

### 6.3 系统安全变更

> **注意**: 以下变更是 MPP 数据库运行的必要条件，但会降低系统安全级别。

| 变更 | 原因 |
|------|------|
| 关闭 SELinux | 数据库进程需要自由访问共享内存和文件 |
| 关闭防火墙 | 集群节点间需要大量端口通信 |
| 管理员用户 sudo 免密 | 部署过程需要频繁切换权限 |
| pg_hba.conf 信任所有 IP | 允许远程客户端连接 (生产环境应收紧) |

### 6.4 文件上传

- 使用 `secure_filename()` 处理文件名
- 无上传大小限制 (`MAX_CONTENT_LENGTH = None`)，数据库安装包可能超过 2GB
- 文件保存到 `/tmp/uploads/` 临时目录

---

## 7. Web UI 前端设计

### 7.1 设计原则

- **向导式引导**: 分步操作，降低用户认知负担，每步聚焦单一任务
- **即时校验**: 每步填写完成后进行校验，错误即时反馈，不累积到最后
- **部署前确认**: 执行部署前展示完整配置摘要，用户明确看到"将要做什么"
- **状态可见**: 部署过程中清晰展示当前阶段、进度和实时日志

### 7.2 页面结构

单页应用 (SPA)，四步向导 (Wizard) 模式，顶部步骤条指示当前进度:

```
┌──────────────────────────────────────────────────────────────┐
│  CBDBTools - MPP 数据库集群部署工具                            │
├──────────────────────────────────────────────────────────────┤
│                                                              │
│  ① 环境配置  ──→  ② 集群配置  ──→  ③ 确认部署  ──→  ④ 执行  │
│  ●              ○              ○              ○              │
│                                                              │
├──────────────────────────────────────────────────────────────┤
│                                                              │
│                     (当前步骤内容区)                           │
│                                                              │
├──────────────────────────────────────────────────────────────┤
│                              [上一步]  [下一步 / 开始部署]    │
└──────────────────────────────────────────────────────────────┘
```

### 7.3 Step 1: 环境配置

选择基础环境参数，所有后续步骤依赖此步的选择。

```
┌──────────────────────────────────────────────────────────────┐
│  操作系统    [CentOS/RHEL ▼]  (自动检测，显示检测结果)         │
│  OS 版本     [8 ▼]           (自动检测)                       │
│                                                              │
│  部署类型    ◉ 单节点   ○ 多节点                               │
│              ↳ Coordinator 和 Segments 在同一台机器            │
│                                                              │
│  安装包路径  [/opt/cloudberry-db-1.6.0-el8.x86_64.rpm    ]   │
│              ✅ 文件存在，格式匹配                              │
│              检测到: Cloudberry DB 1.6.0                      │
│                                                              │
│                                             [下一步 →]       │
└──────────────────────────────────────────────────────────────┘
```

**交互逻辑:**
- 页面加载时调用 `/detect_os` 自动填充操作系统信息
- 安装包路径输入后 (blur 事件) 调用 `/validate_pkg_path` 验证
- 支持从浏览器拖拽或点击上传安装包 (RPM/DEB)，上传到 `/tmp/uploads/`，带实时进度条
- 上传完成后自动填充路径并触发验证，显示检测到的数据库类型和版本
- 部署类型切换影响 Step 2 的表单内容

### 7.4 Step 2: 集群配置

按集群组件分区展示配置项，每个区块对应一个架构组件。Mirror/Standby 通过 checkbox 切换显示/隐藏对应配置区域。底部有显式的"保存配置"按钮。

```
┌──────────────────────────────────────────────────────────────┐
│  ┌─ ADMIN USER ──────────────────────────────────────────┐  │
│  │  用户名  [gpadmin       ]    密码  [••••••••       ]  │  │
│  └───────────────────────────────────────────────────────┘  │
│                                                              │
│  ┌─ COORDINATOR ─────────────────────────────────────────┐  │
│  │  主机名  [mdw           ]    IP    [192.168.1.100  ]  │  │
│  │  端口    [5432          ]    目录  [/data0/.../coord]  │  │
│  └───────────────────────────────────────────────────────┘  │
│                                                              │
│  ┌─ SEGMENT HOSTS (多节点时显示) ────────────────────────┐  │
│  │  访问方式  ◉ 密钥  ○ 密码    访问用户 [root      ]   │  │
│  │  密钥路径  [/root/.ssh/id_rsa                     ]   │  │
│  │  ┌────────────────┬─────────────┬───┐                 │  │
│  │  │ IP             │ 主机名       │   │                 │  │
│  │  │ 192.168.1.101  │ sdw1        │ ✕ │                 │  │
│  │  │ 192.168.1.102  │ sdw2        │ ✕ │                 │  │
│  │  └────────────────┴─────────────┴───┘                 │  │
│  │  [+ 添加 Segment 主机]                                │  │
│  └───────────────────────────────────────────────────────┘  │
│                                                              │
│  ┌─ PRIMARY SEGMENTS ────────────────────────────────────┐  │
│  │  数据目录 [/data0/.../primary]  每主机 [2]  端口 [6000] │  │
│  │  前缀 [gpseg]       集群名称 [CBDB_SANDBOX]           │  │
│  └───────────────────────────────────────────────────────┘  │
│                                                              │
│  ┌─ [✓] MIRROR SEGMENTS ─── (勾选后展开) ───────────────┐  │
│  │  镜像目录 [/data0/.../mirror]   镜像端口 [7000]       │  │
│  │  提示: 每个 Primary 会有对应的 Mirror 副本             │  │
│  └───────────────────────────────────────────────────────┘  │
│                                                              │
│  ┌─ [ ] STANDBY COORDINATOR ─── (勾选后展开) ───────────┐  │
│  │  提示: 自动在 Segment 主机上创建 Standby              │  │
│  └───────────────────────────────────────────────────────┘  │
│                                                              │
│  ┌─ OPTIONS ─────────────────────────────────────────────┐  │
│  │  时区 [Asia/Shanghai]                                 │  │
│  │  [ ] 跳过软件源配置  [ ] 仅初始化环境  [ ] 跳过安装    │  │
│  └───────────────────────────────────────────────────────┘  │
│                                                              │
│  ┌─────────────────────────────────────────────────────────┐│
│  │  ✅ 配置已保存                [保存配置]                ││
│  └─────────────────────────────────────────────────────────┘│
│                                                              │
│                         [← 上一步]  [下一步 →] (保存后启用)  │
└──────────────────────────────────────────────────────────────┘
```

**交互逻辑:**
- 按集群组件分区: Admin User → Coordinator → Segment Hosts → Primary → Mirror → Standby → Options
- **Mirror Segments**: checkbox 勾选后展开镜像目录和端口配置，取消勾选则隐藏
- **Standby Coordinator**: checkbox 勾选后展开说明，取消勾选则隐藏
- **Segment Hosts**: 仅多节点时显示，支持动态增删行
- **显式保存按钮**: 用户点击"保存配置"后显示保存成功/失败反馈
- **"下一步"按钮**: 保存成功后才可点击，否则禁用
- 保存时 Mirror 数据目录会根据 Primary 的 Segments Per Host 数量自动生成对应数量

### 7.5 Step 3: 确认部署

**核心功能: 让用户在执行前完整审阅部署配置。** 所有信息只读展示，发现问题可返回修改。

```
┌──────────────────────────────────────────────────────────────┐
│                                                              │
│  ┌─ 环境信息 ─────────────────────────────────────────────┐  │
│  │  操作系统       CentOS/RHEL 8                          │  │
│  │  部署模式       多节点                                  │  │
│  │  安装包         /opt/cloudberry-db-1.6.0-el8.rpm       │  │
│  │  数据库类型     Cloudberry DB 1.6.0                    │  │
│  │  安装路径       /usr/local/cloudberry-db               │  │
│  └────────────────────────────────────────────────────────┘  │
│                                                              │
│  ┌─ 集群拓扑 ─────────────────────────────────────────────┐  │
│  │                                                        │  │
│  │   ┌──────────────────────┐                             │  │
│  │   │ Coordinator: mdw     │                             │  │
│  │   │ 192.168.1.100:5432   │                             │  │
│  │   └──────────┬───────────┘                             │  │
│  │        ┌─────┼─────┐                                   │  │
│  │     ┌──▼──┐┌─▼──┐┌▼───┐                               │  │
│  │     │sdw1 ││sdw2││sdw3│                                │  │
│  │     │.101 ││.102││.103│                                │  │
│  │     └─────┘└────┘└────┘                                │  │
│  │                                                        │  │
│  │  Segment 数量  2 x 3 主机 = 6 个 Primary Segment       │  │
│  │  Mirror        ⚠️ 未启用                                │  │
│  │  Standby       ⚠️ 未启用                                │  │
│  └────────────────────────────────────────────────────────┘  │
│                                                              │
│  ┌─ 数据库参数 ───────────────────────────────────────────┐  │
│  │  管理员用户     gpadmin                                │  │
│  │  数据库端口     5432                                   │  │
│  │  Coordinator    /data0/coordinator/gpseg-1             │  │
│  │  数据目录       /data0/primary (x2)                    │  │
│  │  集群名称       gp                                     │  │
│  │  Segment 端口   6000 起                                │  │
│  └────────────────────────────────────────────────────────┘  │
│                                                              │
│  ┌─ 注意事项 ─────────────────────────────────────────────┐  │
│  │  ⚠️ Mirror 未启用，单节点 Segment 故障将导致数据不可用   │  │
│  │  ⚠️ Standby 未启用，Coordinator 故障需手动恢复          │  │
│  │  ℹ️ 部署将关闭防火墙和 SELinux                          │  │
│  │  ℹ️ 部署将创建系统用户 gpadmin 并配置免密 sudo           │  │
│  └────────────────────────────────────────────────────────┘  │
│                                                              │
│                       [← 返回修改]  [✔ 确认，开始部署]       │
└──────────────────────────────────────────────────────────────┘
```

**交互逻辑:**
- 页面加载时调用 `/preview_config` 获取完整配置摘要和预检测结果
- 集群拓扑区域以可视化方式展示节点关系，直观呈现部署架构
- 注意事项区域自动根据配置生成提示:
  - 未启用 Mirror/Standby 时给出风险警告
  - 提示部署过程中的系统变更 (防火墙、SELinux 等)
  - 使用默认密码时提示安全风险
- "返回修改"可回到 Step 1 或 Step 2 任意步骤
- "确认，开始部署"调用 `/deploy` 启动部署

### 7.6 Step 4: 部署执行

部署过程中的监控界面，用户可实时跟踪进度。

```
┌──────────────────────────────────────────────────────────────┐
│                                                              │
│  部署状态:  🔄 部署进行中...                                  │
│                                                              │
│  ┌─ 执行进度 ─────────────────────────────────────────────┐  │
│  │  ✅ 环境初始化         系统参数、依赖包、用户创建        │  │
│  │  ✅ 安装数据库软件     Cloudberry DB 1.6.0              │  │
│  │  🔄 Segment 节点初始化  3/3 节点并行执行中...            │  │
│  │  ○  集群创建           gpinitsystem                     │  │
│  │  ○  配置完成           pg_hba / 环境变量                │  │
│  └────────────────────────────────────────────────────────┘  │
│                                                              │
│  ┌─ 实时日志 ─────────────────────────────────────────────┐  │
│  │  [2026-03-19 10:32:15] Configuring kernel parameters...│  │
│  │  [2026-03-19 10:32:16] Setting sysctl params...        │  │
│  │  [2026-03-19 10:32:18] Configuring SSH daemon...       │  │
│  │  [2026-03-19 10:32:20] Creating user gpadmin...        │  │
│  │  [2026-03-19 10:32:22] Installing cloudberry-db RPM... │  │
│  │  [2026-03-19 10:32:45] Starting segment node setup...  │  │
│  │  █                                          (自动滚动) │  │
│  └────────────────────────────────────────────────────────┘  │
│                                                              │
└──────────────────────────────────────────────────────────────┘

─── 部署成功后 ─────────────────────────────────────────────────

┌──────────────────────────────────────────────────────────────┐
│                                                              │
│  部署状态:  ✅ 部署成功!                                      │
│                                                              │
│  ┌─ 连接信息 ─────────────────────────────────────────────┐  │
│  │  主机      192.168.1.100                               │  │
│  │  端口      5432                                        │  │
│  │  用户      gpadmin                                     │  │
│  │  数据库    postgres                                    │  │
│  │                                                        │  │
│  │  连接命令:                                             │  │
│  │  psql -h 192.168.1.100 -p 5432 -U gpadmin postgres    │  │
│  └────────────────────────────────────────────────────────┘  │
│                                                              │
│  [查看完整日志]                       [重新部署]              │
│                                                              │
└──────────────────────────────────────────────────────────────┘
```

**交互逻辑:**
- 通过 SSE (`/stream_logs`) 实时接收日志，自动滚动到底部
- 解析日志关键字匹配当前执行阶段，更新进度指示器
- 部署成功后展示数据库连接信息，方便用户直接使用
- 部署失败时高亮错误信息，提供"查看完整日志"和"重新部署"选项
- "重新部署"调用 `/reset` 后回到 Step 1

### 7.7 步骤间导航规则

| 动作 | 规则 |
|------|------|
| Step 1 → 2 | 安装包路径验证通过后可进入 |
| Step 2 → 3 | 所有必填字段校验通过、`/save_config` 成功后可进入 |
| Step 3 → 4 | 用户点击"确认，开始部署"后进入，不可自动跳转 |
| Step 3 → 1 或 2 | 可自由返回任意步骤修改 |
| Step 4 → 1 | 仅部署完成 (成功或失败) 后可通过"重新部署"回到 Step 1 |
| Step 4 (进行中) | 部署执行中不可切换步骤，防止误操作 |

### 7.8 技术栈

- **后端**: Flask + Gunicorn (`--workers 1 --threads 4`)
  > 必须使用单进程多线程模式。部署状态 (`DEPLOYMENT_STATUS`) 和配置 (`SAVED_CONFIG`) 存储在进程内全局变量中，多 worker 进程会导致状态隔离，SSE 流无法读取部署线程写入的状态。
- **CSS**: Tailwind 风格内联样式，暗色主题，渐变背景
- **JavaScript**: 原生 JS，无框架依赖
- **实时通信**: Server-Sent Events (EventSource API)
- **布局**: 响应式设计

---

## 8. 并行执行设计

### 8.1 multissh.sh

- 并行在多台主机上执行命令
- 默认并发数: 5
- 支持密码/密钥认证
- 锁机制防止输出交错
- 彩色输出 (红色错误/绿色成功/黄色警告)

### 8.2 multiscp.sh

- 并行向多台主机分发文件
- 与 multissh.sh 相同的认证和并发控制
- 保持文件权限

### 8.3 多节点部署并行策略

```
Coordinator (串行)
 ├─ 环境初始化
 ├─ 安装数据库
 └─ 分发文件到所有 Segment
      │
      ├─ Segment 1 ─┐
      ├─ Segment 2 ─┤  并行执行 init_env_segment.sh
      ├─ Segment 3 ─┤
      └─ Segment N ─┘
      │
      └─ 收集/分发 SSH 密钥 (串行)
```

---

## 9. 错误处理与日志

### 9.1 日志策略

| 场景 | 日志方式 |
|------|----------|
| CLI 部署 | `deploy_cluster_YYYYMMDD_HHMMSS.log` 文件 |
| Web UI | SSE 实时推送 + 本地日志文件读取 |
| 所有脚本 | `log_time()` 带时间戳输出 |

### 9.2 错误处理

| 机制 | 说明 |
|------|------|
| `set -o pipefail` | 管道中任何命令失败即终止 |
| 进程防重复 | run.sh 检查已有部署进程 |
| gpinitsystem 容错 | 退出码 1 视为警告，继续执行 |
| SSH 连接失败 | 捕获并记录，返回错误信息 |
| 部署超时 | Web UI 最长监控 1 小时 |
| 线程安全 | `threading.RLock` 保护全局状态；部署状态在路由中初始化 (非线程中)，避免竞态 |

---

## 10. 辅助工具

### 10.1 mirrorlessfailover.sh

- 无 Mirror 场景下的故障转移工具
- 将 Segment 从一个节点迁移到另一个节点

---

## 11. 测试设计

### 11.1 测试覆盖

`test_web.py` 使用 pytest，覆盖以下模块:

| 测试类别 | 覆盖内容 |
|----------|----------|
| **输入验证** | IP / 主机名 / 端口 / 路径 的正确和边界情况 |
| **配置解析** | deploycluster_parameter.sh 和 segmenthosts.conf 的解析 |
| **配置生成** | 参数文件生成、log_time 函数位置、特殊字符转义 |
| **主机文件生成** | Coordinator + Segment 条目格式 |
| **路由测试** | 各 API 端点的参数验证和安全检查 |

### 11.2 测试不足

- 无 Shell 脚本单元测试
- 无端到端集成测试
- 无 multissh/multiscp 测试
- Web UI 前端无自动化测试

---

## 12. 已知限制与改进建议

### 12.1 当前限制

| 编号 | 限制 | 影响 |
|------|------|------|
| L1 | pg_hba.conf 信任所有 IP | 安全风险，生产环境需手动收紧 |
| L2 | 软件源默认使用华为镜像 (RHEL 系) | 非中国区用户需设 `MANUAL_REPO=true` |
| L3 | 部署超时硬编码 1 小时 | 大规模集群可能不够 |
| L4 | 同一时间只能运行一个部署 | 全局 `DEPLOYMENT_STATUS` + 单进程架构限制 |
| L5 | deploycluster.sh 修改自身参数文件 | 副作用不明显，可能导致配置混乱 |
| L6 | sshpass 源码包内嵌仓库 | 版本更新不方便 |
| L7 | 密码明文写入 `deploycluster_parameter.sh` | 部署完成后应考虑清理或限制文件权限 |

### 12.2 改进建议

| 优先级 | 建议 | 说明 |
|--------|------|------|
| P0 | pg_hba.conf 安全加固 | 增加 `PG_HBA_TRUST_RANGE` 配置参数，默认 `0.0.0.0/0`，用户可收紧为子网范围 |
| P1 | 配置文件权限加固 | 部署完成后对含密码的 `deploycluster_parameter.sh` 设置 `chmod 600` |
| P3 | 配置文件独立 | deploycluster.sh 不应运行时修改参数文件 |

---

## 13. 部署拓扑示例

### 单节点

```
┌────────────────────────────────────┐
│     Coordinator 服务器              │
│                                    │
│  Web UI (port 5000, 可选)          │
│  Coordinator (port 5432)           │
│  Segment 1  (port 6000)            │
│  Segment 2  (port 6001)            │
│  ...                               │
└────────────────────────────────────┘
```

### 多节点

```
┌────────────────────────────────────┐
│   Coordinator (mdw)                │
│                                    │
│   Web UI (port 5000, 可选)         │
│   DB Coordinator (port 5432)       │
│   可选: Standby Coordinator        │
└───────────────┬────────────────────┘
                │ SSH 免密
        ┌───────┼───────┐
        │       │       │
   ┌────▼─┐ ┌──▼──┐ ┌──▼──┐
   │ sdw1 │ │ sdw2│ │ sdw3│  Segment 主机
   │ Seg  │ │ Seg │ │ Seg │
   │[Mirror]│[Mirror]│[Mirror]│
   └──────┘ └─────┘ └─────┘
```

---

## 附录 A: 文件依赖关系

```
run.sh
├── deploycluster_parameter.sh (source)
└── deploycluster.sh
    ├── deploycluster_parameter.sh (source + write)
    ├── init_env.sh
    │   ├── common.sh (source)
    │   ├── deploycluster_parameter.sh (source)
    │   ├── segmenthosts.conf (read)
    │   ├── multissh.sh (exec, multi-node)
    │   └── multiscp.sh (exec, multi-node)
    └── init_cluster.sh
        ├── deploycluster_parameter.sh (source)
        └── segmenthosts.conf (read, multi-node)

cluster_deploy_web.py
├── templates/index.html (render)
├── wsgi.py (WSGI entry)
└── 本地调用: deploycluster.sh + 配置文件生成
```

## 附录 B: 关键运行时文件

| 路径 | 用途 | 生命周期 |
|------|------|----------|
| `/tmp/gpinitsystem_config` | gpinitsystem 配置文件 | 部署期间 |
| `/tmp/hostfile_gpinitsystem` | 集群机器列表 | 部署期间 |
| `/tmp/hostsfile` | /etc/hosts 追加内容 | 部署期间 |
| `/tmp/{user}/` | 多节点文件分发暂存 | 部署期间 |
| `~/deploy_cluster_*.log` | 部署日志 | 永久保留 |
