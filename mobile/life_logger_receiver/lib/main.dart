import 'dart:async';

import 'package:flutter/material.dart';

import 'src/receiver_controller.dart';

void main() {
  WidgetsFlutterBinding.ensureInitialized();
  runApp(const LifeLoggerReceiverApp());
}

class LifeLoggerReceiverApp extends StatefulWidget {
  const LifeLoggerReceiverApp({super.key});

  @override
  State<LifeLoggerReceiverApp> createState() => _LifeLoggerReceiverAppState();
}

class _LifeLoggerReceiverAppState extends State<LifeLoggerReceiverApp> {
  late final ReceiverController _controller = ReceiverController();

  @override
  void initState() {
    super.initState();
    unawaited(_controller.initialize());
  }

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => MaterialApp(
        title: 'Life Logger Receiver',
        debugShowCheckedModeBanner: false,
        theme: ThemeData(
          colorScheme: ColorScheme.fromSeed(seedColor: const Color(0xff315f9c)),
          useMaterial3: true,
        ),
        home: ReceiverHomePage(controller: _controller),
      );
}

class ReceiverHomePage extends StatefulWidget {
  const ReceiverHomePage({required this.controller, super.key});

  final ReceiverController controller;

  @override
  State<ReceiverHomePage> createState() => _ReceiverHomePageState();
}

class _ReceiverHomePageState extends State<ReceiverHomePage> {
  late final TextEditingController _lanUrl = TextEditingController();
  late final TextEditingController _tailscaleUrl = TextEditingController();
  bool _addressesLoaded = false;

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    _copyStoredAddresses();
  }

  void _copyStoredAddresses() {
    final controller = widget.controller;
    if (_addressesLoaded || !controller.initialized) return;
    _lanUrl.text = controller.lanMacUrl;
    _tailscaleUrl.text = controller.tailscaleMacUrl;
    _addressesLoaded = true;
  }

  @override
  void dispose() {
    _lanUrl.dispose();
    _tailscaleUrl.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => AnimatedBuilder(
        animation: widget.controller,
        builder: (context, _) {
          _copyStoredAddresses();
          final controller = widget.controller;
          final listening = controller.isListening;
          return Scaffold(
            appBar: AppBar(title: const Text('Life Logger Receiver')),
            body: SafeArea(
              child: ListView(
                padding: const EdgeInsets.fromLTRB(16, 8, 16, 32),
                children: <Widget>[
                  _ReceiverStatusCard(
                    listening: listening,
                    busy: controller.isStarting,
                    failed: controller.isFailed,
                    message: controller.receiverMessage,
                    lastPeer: controller.lastPeer,
                  ),
                  const SizedBox(height: 12),
                  FilledButton.icon(
                    onPressed: !controller.initialized || controller.isStarting
                        ? null
                        : listening
                            ? controller.stopReceiver
                            : controller.startReceiver,
                    icon: Icon(listening ? Icons.stop_circle_outlined : Icons.wifi_tethering),
                    label: Text(listening ? '停止接收' : '启动接收器'),
                  ),
                  const SizedBox(height: 12),
                  _SessionCard(controller: controller),
                  const SizedBox(height: 12),
                  _MacSyncCard(
                    controller: controller,
                    lanUrlController: _lanUrl,
                    tailscaleUrlController: _tailscaleUrl,
                    onSave: () => controller.saveMacEndpoints(
                      directLanUrl: _lanUrl.text,
                      tailscaleUrl: _tailscaleUrl.text,
                    ),
                  ),
                  const SizedBox(height: 12),
                  const _PrivacyAndLimitsCard(),
                ],
              ),
            ),
          );
        },
      );
}

class _ReceiverStatusCard extends StatelessWidget {
  const _ReceiverStatusCard({
    required this.listening,
    required this.busy,
    required this.failed,
    required this.message,
    required this.lastPeer,
  });

  final bool listening;
  final bool busy;
  final bool failed;
  final String message;
  final String? lastPeer;

  @override
  Widget build(BuildContext context) {
    final color = failed
        ? Theme.of(context).colorScheme.error
        : listening
            ? Colors.green.shade700
            : Theme.of(context).colorScheme.outline;
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: <Widget>[
            Row(
              children: <Widget>[
                Icon(
                  listening ? Icons.wifi : Icons.wifi_off,
                  color: color,
                ),
                const SizedBox(width: 10),
                Expanded(
                  child: Text(
                    busy
                        ? '正在启动'
                        : failed
                            ? '接收器异常'
                            : listening
                                ? '等待 AI Passport'
                                : '接收器已停止',
                    style: Theme.of(context).textTheme.titleMedium,
                  ),
                ),
                if (busy) const SizedBox.square(dimension: 20, child: CircularProgressIndicator(strokeWidth: 2)),
              ],
            ),
            const SizedBox(height: 8),
            Text(message),
            const SizedBox(height: 10),
            const Wrap(
              spacing: 8,
              runSpacing: 8,
              children: <Widget>[
                _PortChip(label: 'HTTP 8766'),
                _PortChip(label: 'UDP 8767'),
              ],
            ),
            if (lastPeer != null) ...<Widget>[
              const SizedBox(height: 8),
              Text('最近设备地址：$lastPeer'),
            ],
          ],
        ),
      ),
    );
  }
}

class _PortChip extends StatelessWidget {
  const _PortChip({required this.label});
  final String label;

  @override
  Widget build(BuildContext context) => Chip(
        avatar: const Icon(Icons.lan_outlined, size: 16),
        label: Text(label),
        visualDensity: VisualDensity.compact,
      );
}

class _SessionCard extends StatelessWidget {
  const _SessionCard({required this.controller});
  final ReceiverController controller;

  @override
  Widget build(BuildContext context) => Card(
        child: Padding(
          padding: const EdgeInsets.all(16),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: <Widget>[
              Text('当前会话', style: Theme.of(context).textTheme.titleMedium),
              const SizedBox(height: 12),
              _InfoRow(label: '状态', value: controller.sessionState),
              _InfoRow(label: '设备', value: controller.device ?? '等待设备'),
              _InfoRow(label: 'Session', value: controller.session ?? '—'),
              _InfoRow(label: '唯一音频块', value: '${controller.receivedChunks}'),
              _InfoRow(label: '事件标记', value: '${controller.eventCount}'),
              if (controller.expectedChunks > 0)
                _InfoRow(
                  label: '结束清单',
                  value: '${controller.expectedChunks} 块，缺 ${controller.missingChunks} 块',
                ),
              if (controller.lastActivity != null)
                _InfoRow(
                  label: '最近活动',
                  value: controller.lastActivity!.toLocal().toString().split('.').first,
                ),
            ],
          ),
        ),
      );
}

class _MacSyncCard extends StatelessWidget {
  const _MacSyncCard({
    required this.controller,
    required this.lanUrlController,
    required this.tailscaleUrlController,
    required this.onSave,
  });

  final ReceiverController controller;
  final TextEditingController lanUrlController;
  final TextEditingController tailscaleUrlController;
  final VoidCallback onSave;

  @override
  Widget build(BuildContext context) => Card(
        child: Padding(
          padding: const EdgeInsets.all(16),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: <Widget>[
              Text('同步到 Mac', style: Theme.of(context).textTheme.titleMedium),
              const SizedBox(height: 4),
              Text('局域网地址优先；不可达时尝试 Tailscale 地址。'),
              const SizedBox(height: 12),
              TextField(
                controller: lanUrlController,
                keyboardType: TextInputType.url,
                decoration: const InputDecoration(
                  labelText: 'Mac 局域网地址',
                  hintText: 'http://192.168.1.20:8766',
                  border: OutlineInputBorder(),
                ),
              ),
              const SizedBox(height: 10),
              TextField(
                controller: tailscaleUrlController,
                keyboardType: TextInputType.url,
                decoration: const InputDecoration(
                  labelText: 'Mac Tailscale 地址',
                  hintText: 'http://100.x.y.z:8766',
                  border: OutlineInputBorder(),
                ),
              ),
              const SizedBox(height: 8),
              Wrap(
                spacing: 8,
                children: <Widget>[
                  OutlinedButton.icon(
                    onPressed: onSave,
                    icon: const Icon(Icons.save_outlined),
                    label: const Text('保存地址'),
                  ),
                  FilledButton.tonalIcon(
                    onPressed: controller.syncing ? null : controller.syncNow,
                    icon: controller.syncing
                        ? const SizedBox.square(
                            dimension: 16,
                            child: CircularProgressIndicator(strokeWidth: 2),
                          )
                        : const Icon(Icons.sync),
                    label: const Text('立即同步'),
                  ),
                ],
              ),
              const SizedBox(height: 8),
              _InfoRow(label: '待 Mac 确认', value: '${controller.pendingSyncItems} 项'),
              _InfoRow(label: '同步状态', value: controller.syncMessage),
              if (controller.lastSyncRoute != null)
                _InfoRow(label: '最近通道', value: controller.lastSyncRoute!),
              if (controller.lastSyncAt != null)
                _InfoRow(
                  label: '最近同步',
                  value: controller.lastSyncAt!.toLocal().toString().split('.').first,
                ),
            ],
          ),
        ),
      );
}

class _InfoRow extends StatelessWidget {
  const _InfoRow({required this.label, required this.value});
  final String label;
  final String value;

  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.only(top: 6),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: <Widget>[
            SizedBox(
              width: 104,
              child: Text(label, style: Theme.of(context).textTheme.bodySmall),
            ),
            Expanded(child: Text(value)),
          ],
        ),
      );
}

class _PrivacyAndLimitsCard extends StatelessWidget {
  const _PrivacyAndLimitsCard();

  @override
  Widget build(BuildContext context) => Card(
        color: Theme.of(context).colorScheme.surfaceContainerHighest,
        child: const Padding(
          padding: EdgeInsets.all(16),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: <Widget>[
              Text('本机缓存与运行限制', style: TextStyle(fontWeight: FontWeight.w600)),
              SizedBox(height: 8),
              Text('录音保存在手机应用目录，排除系统云备份。CRC32 用于完整性校验，不是身份认证。'),
              SizedBox(height: 6),
              Text('iOS 接收需本地网络授权和 Apple 批准的多播 entitlement；应用进入后台时暂停接收。'),
              SizedBox(height: 6),
              Text('HTTP 传输没有应用层加密或认证；跨网同步应使用已连接的 Tailscale。'),
            ],
          ),
        ),
      );
}
