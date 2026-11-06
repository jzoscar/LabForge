# LabForge

用 IaC（基础设施即代码）编排和监控 **开发 / 测试 / 研发 / 非生产** 环境的测试设备管理平台。
一个 Python 工程，同时提供：

- **IaC 引擎**：YAML 声明环境 → `plan` 计算差异 → `apply` 收敛真实设施（类似 Terraform 的状态 + 计划模型）
- **后台**：FastAPI REST API（`/api/v1`，OpenAPI 文档在 `/docs`）+ 后台监控调度
- **前台**：服务端渲染的 Web 控制台（总览 / 资源 / IaC 计划 / 执行记录），无需 Node 构建
- **Agent**：只依赖 `psutil` 的单文件采集器，可直接拷贝到 **ARM Linux 板卡** 和 **Windows** 测试机

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
| **M0 平台骨架** | ✅ 已完成（v0.1.0） | IaC 声明、plan/apply、各资源驱动、监控巡检、Agent、Web 控制台、测试 |
| **M1 接入真实实验室** | 🚧 进行中 | 把开发环境的真实 ARM 板、Windows 机、服务器、网络接入并稳定运行 |
| **M2 安全与多人协作** | ⏳ 计划中 | 多人可以安全地共用一套平台 |
| **M3 测试设备预约** | ⏳ 计划中 | 测试硬件可以排期、独占、被 CI 自动借用 |
| **M4 告警与可观测** | ⏳ 计划中 | 问题能主动通知到人，指标进入统一监控体系 |
| **M5 网络深度监控** | ⏳ 计划中 | 交换机、链路、电源层面也纳入监控与控制 |
| **M6 生产环境** | ⏳ 计划中 | 满足生产变更的审批与隔离要求 |

### M0 平台骨架 ✅

- [x] IaC 模式：环境 / 网络 / 负载均衡 / 主机 / 容器，含交叉引用校验
- [x] plan / apply / destroy，依赖拓扑排序，状态存储，dry-run
- [x] 驱动：Redfish · IPMI · SSH · WinRM · Docker · HAProxy · Terraform
- [x] 监控：ping / TCP / HTTP / BMC / 容器 / LB 后端 / 网关，状态事件
- [x] Agent（ARM Linux / Windows），阈值告警，架构与系统漂移检测
- [x] Web 控制台 + REST API，生产环境护栏，自动化测试

### M1 接入真实实验室 🚧

- [ ] 盘点开发环境的设备，把 `infra/dev.yaml` 改成真实的地址、BMC、账号
- [ ] 平台服务部署到实验室内一台常驻服务器，开启 `LABFORGE_API_TOKEN`
- [ ] 所有 ARM 板和 Windows 机装好 Agent 并持续上报
- [ ] 对 `dev` 做一次真实 apply，BMC 电源控制与初始化命令跑通
- [ ] **验收**：总览页里 dev 环境全部“正常”，没有漂移、没有失联 Agent，连续运行一周
- [ ] 按同样方式接入 `rnd`、`staging`

### M2 安全与多人协作

- [ ] 数据库切换到 PostgreSQL
- [ ] 用户登录（LDAP / OIDC 单点登录）与按环境的 RBAC
- [ ] 审计日志：谁、何时、对哪个环境做了什么
- [ ] 每台 Agent 独立令牌，可单独吊销
- [ ] IaC 放进 git 仓库：提 PR 自动出 plan，合并后自动 apply（GitOps）

### M3 测试设备预约

- [ ] 设备预约 / 借用 / 归还，带时间段与独占锁
- [ ] 预约日历视图
- [ ] API 供 CI 流水线自动借用板卡、跑完自动归还
- [ ] 归还时自动断电重启或重装系统（BMC + PXE）

### M4 告警与可观测

- [ ] 告警通知：邮件 / 企业微信 / 飞书 / Webhook，支持静默与升级
- [ ] `/metrics` 导出 Prometheus 指标，提供 Grafana 面板
- [ ] 历史趋势图与可用率报表

### M5 网络深度监控

- [ ] SNMP 采集交换机端口状态与流量
- [ ] PDU 远程断电 / 上电（没有 BMC 的板卡也能硬重启）
- [ ] 负载均衡连接数、错误率指标

### M6 生产环境

- [ ] 变更审批流与变更窗口
- [ ] 生产环境的独立权限与网络隔离
- [ ] 回滚与变更前自动备份

## 上手指南：从演示到接入真实实验室（M1）

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
