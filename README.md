# zhishi-a-share-data
知势 A 股研究台的 AKShare 免费行情快照与定时采集任务

## 数据更新时间

- 交易日 11:42（Asia/Shanghai）：生成午盘快照。
- 交易日 15:37（Asia/Shanghai）：生成收盘快照。
- 如果收盘数据尚未完整，任务会在 15:47、15:57 自动重试。
- 节假日或抓取失败不会覆盖上一份有效快照。

网页顶部会直接显示行情交易日、午盘/收盘状态和数据生成时间。GitHub Actions 的定时任务可能因平台排队延迟几分钟。

## 手动更新

1. 打开仓库的 `Actions` 页面。
2. 选择 `A-share market snapshot`。
3. 点击 `Run workflow`。
4. 收盘后选择 `close`，午盘后选择 `midday`，再确认运行。
5. 任务出现绿色对勾后，等待 GitHub Pages 部署完成，再刷新网页。

手动更新入口：<https://github.com/gdsgsdhdsv/zhishi-a-share-data/actions/workflows/market-snapshot.yml>
