# USB Transfer - 飞牛OS USB文件传输插件

一个用于飞牛OS (fnOS) 的原生插件，自动检测USB存储设备并将照片/视频导入到NAS指定目录。

## 功能

- **自动检测USB设备** - 插入USB后3秒内自动识别
- **DCIM自动导入** - 自动发现相机/手机存储卡中的DCIM目录并导入
- **可配置监听目录** - 不仅限于DCIM，可自定义添加 Photos、Video 等目录名
- **三种传输模式**
  - 复制：完整复制所有文件
  - 增量同步：只传输新增/修改的文件，跳过已存在的
  - 镜像同步：让目标与源完全一致
- **实时进度显示** - 传输百分比、速度、当前文件名、文件计数
- **传输历史记录** - 自动记录每次传输的结果
- **Web界面** - Tab式布局，自动导入 + 手动传输 + 历史记录

## 截图

打开Web界面后包含三个Tab：

| Tab | 功能 |
|-----|------|
| DCIM 自动导入 | 设置目标目录、开关自动传输、查看检测日志 |
| 手动传输 | 双面板目录浏览器，选择源和目标后手动传输 |
| 历史记录 | 查看所有传输记录（时间、路径、模式、耗时、状态） |

## 安装

### 方式一：下载安装包

1. 从 [Releases](https://github.com/JanStarzz/usb-transfer/releases) 下载最新的 `.fpk` 文件
2. 打开飞牛OS → 应用中心 → 手动安装
3. 选择下载的 `.fpk` 文件

### 方式二：自行打包

```bash
git clone https://github.com/JanStarzz/usb-transfer.git
cd usb-transfer
bash build.sh
# 生成 usb-transfer_1.0.0_x86.fpk
```

将生成的 `.fpk` 上传到飞牛NAS手动安装。

## 使用方法

### 自动导入（推荐）

1. 安装后打开应用，进入「DCIM 自动导入」Tab
2. 点击「选择目录」设置照片存储的目标路径（如 `/vol1/photos`）
3. 打开「自动导入」开关
4. 插入含有 DCIM 目录的USB设备（相机存储卡、手机等）
5. 等待几秒，自动开始增量同步传输

### 手动传输

1. 切换到「手动传输」Tab
2. 左侧选择USB设备和源目录
3. 右侧选择NAS目标目录
4. 选择传输模式，点击开始

## 技术架构

- **后端**：Python 3 标准库 HTTP Server（无外部依赖）
- **前端**：Vue 3 CDN 单页面应用
- **文件传输**：rsync（支持增量同步和进度追踪）
- **USB检测**：lsblk + /proc/mounts
- **默认端口**：8580

## 项目结构

```
usb-transfer/
├── fnos/
│   ├── manifest                  # 应用元数据
│   ├── ICON.PNG / ICON_256.PNG   # 应用图标
│   ├── UsbTransfer.sc            # 防火墙端口规则
│   ├── app/
│   │   ├── server.py             # Python HTTP 后端
│   │   └── static/index.html     # Vue 3 前端
│   ├── bin/usb-transfer-server   # 启动脚本
│   ├── cmd/service-setup         # fnOS 生命周期管理
│   ├── config/
│   │   ├── privilege             # 运行权限（root）
│   │   └── resource              # 端口配置
│   ├── ui/
│   │   ├── config                # 桌面启动器
│   │   └── images/               # 图标
│   └── wizard/config             # 安装向导
└── build.sh                      # 一键打包脚本
```

## SSH 调试

```bash
# 查看服务状态
/var/apps/usb-transfer/cmd/main status

# 手动启动
/var/apps/usb-transfer/cmd/main start

# 查看日志
/var/apps/usb-transfer/cmd/main log

# 直接访问 Web UI
curl http://localhost:8580
```

## 系统要求

- 飞牛OS (fnOS)
- Python 3（系统自带）
- rsync（系统自带）

## 许可证

MIT
