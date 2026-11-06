# LabForge

用 IaC（基础设施即代码）编排和监控 **开发 / 测试 / 研发 / 非生产** 环境的测试设备管理平台。
一个 Python 工程，同时提供：

- **IaC 引擎**：YAML 声明环境 → `plan` 计算差异 → `apply` 收敛真实设施（类似 Terraform 的状态 + 计划模型）
- **后台**：FastAPI REST API（`/api/v1`，OpenAPI 文档在 `/docs`）+ 后台监控调度
- **前台**：服务端渲染的 Web 控制台（总览 / 资源 / IaC 计划 / 执行记录），无需 Node 构建
- **Agent**：只依赖 `psutil` 的单文件采集器，可直接拷贝到 **ARM Linux 板卡** 和 **Windows** 测试机

## 定位：构建流水线背后的“硬件实验室”

LabForge 面向的是嵌入式 / 工业传感器产品的构建与测试团队。这类团队的代码要用交叉编译的 ARM GCC 和 MSVC 构建，最终跑在真实设备上，**一次有问题的发布会让客户的生产线停下来**。

构建本身（编译、缓存、日志、自动排障、SBOM）由姊妹项目 [BuildPulse](https://github.com/jzoscar/BuildPulse) 负责。LabForge 管它背后的东西：

- **设备测试台（test rigs）**：一池 ARM 传感器设备，CI 用之前要先借出并加锁，跑完要恢复到已知良好的镜像
- **构建机群**：Linux Docker 构建主机、Windows MSVC 构建机、Bamboo 远程 agent 和 Bitbucket Pipelines / GitHub Actions 自托管 runner
- **共享基础设施**：Conan 远端仓库（Artifactory）、sccache 远端缓存、制品库、实验室网络、负载均衡

这些全部用 IaC 声明、一键收敛、持续监控。目标是让流水线失败时能立刻分清：**是代码坏了，还是实验室坏了。**

### 与岗位要求的对应

| 岗位关注点 | LabForge 中的对应 |
|---|---|
| 产品跑在真实硬件上，坏的发布会让生产线停摆 | M2 设备测试台：每次发布前在真机上验证；M6 金丝雀发布与自动回滚 |
| ARM 交叉编译 + Windows MSVC 双平台 | 设备池（ARM Linux）与构建机（Windows / Linux）都在 IaC 里按平台建模，Agent 检测架构漂移 |
| Docker 构建环境 | 构建主机与缓存服务以容器形式由 IaC 管理 |
| 把 40 分钟的构建缩短到一杯咖啡的时间 | M4 构建机群容量与远端缓存服务，排队时间与缓存命中率可观测 |
| “修复，而不只是提单”（fix, don't just file） | 失败自动区分基础设施问题与代码问题，设备问题自动隔离和恢复，代码问题交给 BuildPulse triage |
| Bamboo → Bitbucket Pipelines 迁移 | runner / agent 按硬件打标签，两套 CI 都能向 LabForge 借设备 |
| 欧盟 CRA（网络弹性法案） | M5 每台设备的固件版本清单，新 CVE 出现时能查到哪些设备、哪些版本受影响 |
| 在实际工作中用 AI 产出生产级结果 | 失败上下文（设备 ID、固件、健康检查、日志）结构化输出，可直接交给 AI 排障 |

## 能管什么

| 资源 | IaC 段 | 编排能力（driver） | 监控 |
|---|---|---|---|
| 网络 / VPC / VLAN / 物理网段 | `networks` | `record`（登记）· `terraform`（委托已有模块） | 各子网网关 ping |
| 负载均衡 | `loadBalancers` | `record` · `haproxy`（渲染配置经 SSH 下发、校验、reload）· `terraform` | VIP 监听端口 + 每个后端健康检查 |
| 实体硬件（裸金属，ARM / x86） | `hosts` (`kind: baremetal`) | BMC 电源控制（Redfish / IPMI）+ SSH / WinRM 初始化命令 | ping/tcp/http 探针、BMC 健康/电源/温度、Agent 指标 |
| Windows 机器（含 Windows on ARM） | `hosts` (`platform.os: windows`) | WinRM 执行 PowerShell | 同上（WinRM/RDP 端口、Agent） |
| Linux 服务器 / 虚拟机 / 云主机 | `hosts` (`kind: vm/cloud`) | SSH 初始化命令 | 同上 |
| 容器 | `containers` | Docker（本机或 `docker -H ssh://` 远程） | `docker inspect` 运行状态 |

额外的监控能力：

- **漂移检测**：Agent 上报的 OS / CPU 架构与 IaC 声明不一致时（例如 ARM 板被刷成 x86 镜像）标记为降级
- **阈值告警**：CPU > 95%、内存 > 95%、磁盘 > 90%、温度 > 85°C
- **Agent 失联**：超过 `LABFORGE_AGENT_STALE_AFTER` 秒未上报即判定异常
- **状态变化事件**：每次 正常 / 降级 / 故障 / 未知 之间的切换都会记录

## 快速开始（本机演示，无需真实硬件）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'          # Windows 目标机需要 WinRM 时：pip install -e '.[windows]'
```

```bash
export LABFORGE_IAC_PATH=examples/local
labforge plan
labforge apply -y                 # 演示环境只做登记，不会动任何机器
labforge serve                    # 打开 http://127.0.0.1:8000
```

另开一个终端，让本机作为一台“测试硬件”上报：

```bash
labforge agent --env demo --name this-mac
```

## 管理你的实验室

`infra/` 是真实环境的起点模板（`dev.yaml` 开发、`rnd.yaml` 研发、`staging.yaml` 非生产），按实际情况修改地址、BMC、账号后：

```bash
labforge validate infra           # 结构 + 交叉引用校验（子网是否在 CIDR 内、LB 目标是否存在……）
labforge plan infra               # 只看差异
labforge apply infra --dry-run    # 打印将执行的每一条 ssh / ipmitool / docker / terraform 命令
labforge apply infra              # 真实执行（会二次确认）
labforge state                    # 查看所有资源及健康状态
labforge destroy --env rnd        # 拆除一个环境（实体机只会被注销，绝不会被擦除）
```

### 密钥

IaC 中只写引用名，值从环境变量读取：`credentialsRef: bmc-default` → `LABFORGE_SECRET_BMC_DEFAULT`。

| 引用 | 格式 |
|---|---|
| `bmc.credentialsRef` | `user:password` |
| `connection.keyRef` | SSH 私钥文件路径 |
| `connection.passwordRef` | WinRM 密码 |
| `containers[].secretEnv` | 容器环境变量的值（不会出现在命令行或日志中） |

`--dry-run` 不需要任何密钥。IPMI 密码通过 `IPMI_PASSWORD` 环境变量传给 `ipmitool -E`，不会进入命令行和日志。

YAML 中还可以用 `${VAR}` / `${VAR:-默认值}` 引用普通环境变量。

### 安全护栏

- `tier: prod` 的环境默认拒绝变更，必须显式 `--allow-prod`（API: `allow_prod: true`）
- 每次 apply 只作用于所加载文件里出现的环境，不会误删其它环境的状态
- Web 端默认只能 dry-run；真实执行需 `LABFORGE_UI_APPLY=1`
- 设置 `LABFORGE_API_TOKEN` 后，所有写 API 和 Agent 上报都必须带 `X-LabForge-Token`
- 同一服务进程内 apply 串行执行

## IaC 规范速查

```yaml
apiVersion: labforge/v1
kind: Environment
metadata: { name: dev, tier: dev }        # tier: dev | test | rnd | staging | prod
spec:
  networks:
    - name: lab-vpc
      type: vpc                           # vpc | vlan | physical
      cidr: 10.20.0.0/16
      subnets: [ { name: arm, cidr: 10.20.1.0/24, gateway: 10.20.1.1, vlan: 101 } ]
      driver: record                      # record | terraform (+ terraform: {module, vars})
  hosts:
    - name: arm-node-01
      kind: baremetal                     # baremetal | vm | cloud
      platform: { os: linux, arch: arm64 } # os: linux|windows|macos  arch: arm64|amd64|armv7|riscv64
      address: 10.20.1.11
      subnet: lab-vpc/arm
      bmc: { type: redfish, address: 10.20.0.11, credentialsRef: bmc-default }   # redfish | ipmi
      power: "on"                         # 期望电源状态（需要 bmc）
      connection: { type: ssh, user: lab, keyRef: lab-ssh-key }                 # ssh | winrm | none
      bootstrap: [ "sudo apt-get install -y python3-psutil" ]  # 幂等命令；首次创建或内容变化时执行
      monitor:
        agent: true
        probes: [ { type: ping }, { type: tcp, port: 22 }, { type: http, port: 8080, path: /healthz } ]
      onDestroy: deregister               # deregister | poweroff
  loadBalancers:
    - name: test-api
      vip: 10.20.3.100
      driver: haproxy                     # record | haproxy | terraform
      host: svc-01                        # haproxy 运行所在主机（SSH）
      listeners: [ { port: 8080, protocol: http, targetPort: 8080 } ]
      targets: [ arm-node-01 ]
      healthCheck: { protocol: http, port: 8080, path: /healthz }
  containers:
    - name: test-db
      host: svc-01                        # connection: none 的主机表示本机 docker
      image: postgres:16
      ports: ["5432:5432"]
      env: { POSTGRES_DB: testdb }
      secretEnv: { POSTGRES_PASSWORD: test-db-password }
```

未知字段会直接报错（防止拼写错误被静默忽略）。完整例子见 [infra/dev.yaml](infra/dev.yaml)。

## 部署 Agent

Agent 是单文件 [labforge/agent.py](labforge/agent.py)，只依赖 `psutil`，用 `--env` + `--name` 对应 IaC 里的主机。

**ARM / x86 Linux（systemd）**：

```bash
sudo mkdir -p /opt/labforge && sudo cp labforge/agent.py /opt/labforge/
sudo apt-get install -y python3-psutil          # 或 pip install psutil
sudo cp deploy/agent/labforge-agent.service /etc/systemd/system/
sudo systemctl edit labforge-agent               # 填写 LABFORGE_SERVER / LABFORGE_ENV / LABFORGE_HOST / LABFORGE_AGENT_TOKEN
sudo systemctl enable --now labforge-agent
```

**Windows（x64 与 ARM64）**：以管理员 PowerShell 运行 [deploy/agent/install-agent.ps1](deploy/agent/install-agent.ps1)，会注册一个开机自启的计划任务。

温度采集：Linux 读取 `psutil` 传感器，ARM 板卡回退到 `/sys/class/thermal`；Windows 上温度请依赖 BMC（Redfish）。

## 架构

```
            ┌──────────── labforge serve ─────────────┐
 浏览器 ───▶ │ Web UI (Jinja2)   REST API /api/v1      │
 CLI ──────▶ │ service: plan / apply / runs            │
            │ IaC: loader → planner → applier          │──▶ ssh · winrm · redfish · ipmitool · docker · terraform
            │ monitor: 周期探测 + Agent 指标 + 漂移       │──▶ ping · tcp · http · BMC · docker inspect
            └───────────────┬─────────────────────────┘
                            │ SQLite（默认）/ PostgreSQL：状态、执行记录、探测结果、事件、指标
 Agent (ARM/Windows) ───────┘ POST /api/v1/agent/report
```

| 目录 | 作用 |
|---|---|
| `labforge/iac/spec.py` | IaC 模式定义与校验 |
| `labforge/iac/planner.py` | 期望 vs 状态 → 有序变更计划（依赖拓扑排序，删除逆序） |
| `labforge/iac/applier.py` · `providers/` | 执行计划；各资源驱动 |
| `labforge/monitor/` | 异步探针、巡检、状态聚合 |
| `labforge/api.py` · `web.py` · `templates/` | 后台 API 与前台页面 |
| `labforge/agent.py` | 主机 Agent |

## 配置

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `LABFORGE_DB_URL` | `sqlite:///labforge.db` | 多人/多实例使用时换成 PostgreSQL |
| `LABFORGE_IAC_PATH` | `infra` | IaC 文件或目录 |
| `LABFORGE_API_TOKEN` | 无 | 写 API 与 Agent 上报的令牌 |
| `LABFORGE_MONITOR` | `true` | 服务内是否运行巡检 |
| `LABFORGE_MONITOR_INTERVAL` | `30` | 巡检间隔（秒） |
| `LABFORGE_AGENT_STALE_AFTER` | `120` | Agent 失联判定（秒） |
| `LABFORGE_RETENTION_HOURS` | `24` | Agent 指标保留时长（事件保留 7 倍） |
| `LABFORGE_UI_APPLY` | `false` | 允许网页端真实执行 |
| `LABFORGE_DATA_DIR` | `.labforge` | Terraform 变量文件等临时产物 |

## 开发

```bash
pytest
```

## 里程碑

| 里程碑 | 状态 | 目标 |
|---|---|---|
| **M0 平台骨架** | ✅ 已完成（v0.1.0） | IaC、plan/apply、各类驱动、监控、Agent、Web 控制台 |
| **M1 构建与测试实验室接入** | 🚧 下一步 | 把设备池、构建机、runner、缓存服务全部用 IaC 管起来 |
| **M2 设备测试台（CI 真机测试）** | ⏳ 计划中 | CI 能自动借设备、刷固件、跑真机测试、归还并恢复 |
| **M3 告警与值班** | ⏳ 计划中 | 通过 Slack / Teams / WhatsApp / 短信 把问题推给对的人 |
| **M4 构建机群容量与速度** | ⏳ 计划中 | 构建排队少、缓存热，构建时间可度量、可优化 |
| **M5 安全、合规与 GitOps** | ⏳ 计划中 | 多人安全协作，满足 CRA 的追溯要求 |
| **M6 生产发布护栏** | ⏳ 计划中 | 发往客户产线的版本先过真机，再分阶段发布 |

### M0 平台骨架 ✅

- [x] IaC 模式：环境 / 网络 / 负载均衡 / 主机 / 容器，含交叉引用校验
- [x] plan / apply / destroy，依赖拓扑排序，状态存储，dry-run，生产环境护栏
- [x] 驱动：Redfish · IPMI · SSH · WinRM · Docker · HAProxy · Terraform
- [x] 监控：ping / TCP / HTTP / BMC / 容器 / LB 后端 / 网关，状态变化事件
- [x] Agent（ARM Linux / Windows），阈值告警，架构与系统漂移检测
- [x] Web 控制台 + REST API，自动化测试

### M1 构建与测试实验室接入 🚧

- [ ] 在 IaC 中声明整个实验室：
  - 设备池：按产品系列分组的 ARM 传感器设备（带固件版本、序列号标签）
  - 构建机：Linux Docker 构建主机、Windows MSVC 构建机
  - CI 执行器：Bamboo 远程 agent、Bitbucket Pipelines / GitHub Actions 自托管 runner，带硬件标签（如 `rig-sensor-a`）
  - 共享服务：Conan 远端（Artifactory）、sccache 远端缓存、内部镜像仓库
  - 网络：实验室 VLAN（管理网 / 设备网 / 构建网），以及设备前面的负载均衡
- [ ] 构建机专项监控：磁盘余量（构建机最常见的故障）、Docker 磁盘占用、runner 进程是否在线
- [ ] 平台服务部署到实验室常驻服务器，开启 API 令牌
- [ ] 所有设备、构建机装好 Agent；没有 Agent 的设备（嵌入式固件）走 ping / HTTP 版本接口探测
- [ ] **验收**：实验室全部资源在总览页“正常”，没有漂移和失联，连续运行一周

### M2 设备测试台（CI 真机测试）

QEMU 只能证明 ARM 二进制能跑，证明不了产品在设备上能工作。

- [ ] 设备借出 / 归还 API：按标签从设备池里借一台并加锁，带超时自动归还
- [ ] 测试前健康检查：开机、网络、版本接口；失败记为**基础设施失败**，不算测试失败
- [ ] 刷写固件 / 部署构建产物；无论测试成功还是失败，归还时都恢复到已知良好的镜像
- [ ] 设备自愈：BMC 或 **PDU 断电重启**（没有 BMC 的设备也能硬重启），连续两次健康检查失败自动隔离
- [ ] CI 集成：Bamboo、Bitbucket Pipelines、GitHub Actions 的 job 通过同一个 API 借设备
- [ ] 与 BuildPulse 打通：测试结果写进 `report.json`，环境信息带设备 ID 和固件版本，失败走 `buildpulse triage`
- [ ] 测试台看板：设备利用率、排队时长、失败原因分布（代码 / 设备 / 网络），不稳定测试单独跟踪

### M3 告警与值班

- [ ] 通知渠道（可插拔，每个渠道一个适配器）：
  - **Slack**（主渠道）：带操作按钮，可直接确认、静默、隔离设备，告警消息带执行记录链接和失败摘要
  - **Microsoft Teams**：Incoming Webhook / Workflows
  - **WhatsApp**：Meta WhatsApp Cloud API 或 Twilio，用于非工作时间的值班通知
  - **短信**：Twilio，作为最后的兜底（iMessage 没有公开的发送 API，所以用短信覆盖手机）
  - **PagerDuty / Opsgenie**：值班轮换与升级；另有邮件、通用 Webhook
- [ ] 路由规则：按环境层级（dev / test / rnd / staging / prod）和严重程度分发，例如构建机磁盘满 → Slack 频道；设备池可用率低于阈值 → 值班人 WhatsApp
- [ ] 去重、聚合、静默窗口、恢复通知，防止告警风暴
- [ ] `/metrics` 导出 Prometheus 指标，提供 Grafana 面板；历史趋势与可用率报表

### M4 构建机群容量与速度

- [ ] 构建 agent 用 IaC 管理，按需扩缩临时 Docker 构建 agent
- [ ] 读取 BuildPulse 的 `report.json`，统计各阶段耗时、缓存命中率、排队时间
- [ ] 远端缓存服务（Conan 远端、sccache）的容量和命中率监控，所有 runner 一启动就是热缓存
- [ ] 找出最慢的流水线和阶段，给出优化建议

### M5 安全、合规与 GitOps

- [ ] IaC 放进 Bitbucket / GitHub：提 PR 自动出 plan 评论，合并后自动 apply
- [ ] 单点登录（OIDC / LDAP）、按环境的 RBAC、审计日志
- [ ] 每台 Agent 独立令牌；密钥迁到 Vault 等密钥管理服务
- [ ] **CRA 追溯**：记录每台设备当前的固件版本，并关联 BuildPulse 生成的该版本 SBOM；新 CVE 发布时能马上查出受影响的设备和版本，支撑 24 小时上报流程
- [ ] 数据库切换到 PostgreSQL，多实例部署

### M6 生产发布护栏

- [ ] 发布门禁：版本必须在设备测试台的全部产品系列上通过，才能进入发布
- [ ] 分阶段发布：先发到一个内部产线工位（金丝雀），观察健康指标后再扩大
- [ ] 健康指标异常时自动回滚到已知良好的固件
- [ ] 生产环境的变更审批、变更窗口、独立权限和网络隔离

## 上手指南：从演示到接入真实实验室（M1）

下面以开发环境为例；设备池、构建机、runner 按同样的方式写进 IaC。

按顺序做，每一步都有可验证的结果。

**第 1 步：准备运行环境**（需要 Python 3.11+）

```bash
git clone https://github.com/jzoscar/LabForge.git && cd LabForge
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev,windows]'
pytest
```

✅ 看到测试全部通过。

**第 2 步：跑通本机演示**，熟悉界面（见上方“快速开始”）。

✅ 浏览器里能看到 `this-mac` 正常、`offline-board` 故障。

**第 3 步：盘点设备，填写 IaC。** 先只做开发环境，编辑 `infra/dev.yaml`：

- `networks`：实验室网段和各子网（管理网 / ARM 网段 / Windows 网段 / 服务网段）
- `hosts`：每台设备一条，写清 `platform.os` / `platform.arch`、`address`，有 BMC 的写 `bmc`，以及 `connection`
- 暂时用不上的部分（负载均衡、容器）可以先删掉，之后再加

```bash
labforge validate infra/dev.yaml
```

✅ 输出 `✓ dev [dev]: ...`，没有报错。

**第 4 步：配置密钥**（写在服务器的环境变量或 `.env` 里，不要写进 IaC 文件或提交到 git）

```bash
export LABFORGE_SECRET_BMC_DEFAULT='admin:你的BMC密码'
export LABFORGE_SECRET_LAB_SSH_KEY="$HOME/.ssh/lab_ed25519"
export LABFORGE_SECRET_WIN_ADMIN='你的Windows管理员密码'
```

**第 5 步：预演**

```bash
labforge plan infra/dev.yaml
labforge apply infra/dev.yaml --dry-run
```

✅ 逐条检查将要执行的 ssh / ipmitool / winrm / docker 命令是否符合预期。

**第 6 步：部署平台服务**到实验室里一台能访问所有设备和 BMC 的常驻服务器：

```bash
export LABFORGE_API_TOKEN=$(openssl rand -hex 24)   # 记下来，Agent 要用
export LABFORGE_IAC_PATH=infra
export LABFORGE_DB_URL=sqlite:////var/lib/labforge/labforge.db
labforge serve --host 0.0.0.0 --port 8000
```

或者用容器：`docker build -t labforge . && docker run -d -p 8000:8000 -v labforge-data:/data -e LABFORGE_API_TOKEN=... labforge`

✅ 实验室内其它机器能打开 `http://<服务器>:8000`。

**第 7 步：在每台 ARM 板和 Windows 机上装 Agent**（见上方“部署 Agent”），`--name` 必须和 IaC 中的主机名一致。

✅ 总览页里每台主机的 CPU / 内存 / 磁盘有数据，“Agent”列显示几秒前。

**第 8 步：真实执行**

```bash
labforge apply infra/dev.yaml
```

✅ 执行记录显示 success，`labforge state` 列出全部资源。

**第 9 步：验收与排障。** 观察一周，总览页中 dev 全部“正常”。如果出现：

| 现象 | 处理 |
|---|---|
| BMC 列显示“no BMC credentials” | 检查 `LABFORGE_SECRET_*` 是否在 **服务进程** 的环境变量里 |
| 漂移：`arch aarch64 != spec amd64` | 设备实际架构与 IaC 不符，修正 IaC 或排查设备 |
| Agent “无上报” / 失联 | 检查 `--server` 地址、令牌、防火墙 8000 端口 |
| ping 不通但服务正常 | 设备禁 ICMP，把 `monitor.probes` 改成 `tcp` 探针 |

**第 10 步：扩展**到 `infra/rnd.yaml`、`infra/staging.yaml`，然后进入 M2。
