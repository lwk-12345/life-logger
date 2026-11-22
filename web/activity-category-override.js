/* [A0 11-03 21:18 / 22:52] 分类覆盖表：把「对电脑/助手说的话」从"生活·日常"改到"与助手对话"
 * 判据：原话同时含「对人说话」+「在说电脑/项目的事」
 * 注意：这是分类覆盖，不是折叠——这些仍是用户的行为，只是不是"生活日常"
 * 22:52 +12 条：来自 work/six/domestic-64/raw/classified.tsv 的 LIVE_ASSISTANT 判定（逐条读原话）
 */
window.ACTIVITY_CATEGORY_OVERRIDE = {
  "2026-11-02": {
    "assistant": ["act_001","act_002","act_003","act_004","act_006","act_026","act_027","act_031","act_061","act_073","act_074","act_076","act_077","act_099","act_100","act_120","act_127","act_160","act_163"]
  }
};
