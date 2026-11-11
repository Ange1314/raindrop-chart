/* ============================================================
 *  Raindrop Chart · 数据入口
 *  ------------------------------------------------------------
 *  这是唯一的数据位置。换数据只改这个文件，画布不碰。
 *  也可以运行时不加载它，直接调用 window.RaindropChart.setData(结构相同)
 *
 *  结构：
 *    {
 *      unit: '次',                          // 数值单位，只影响文案
 *      flows: [
 *        { name: '需求提出', value: 96, group: '需求管理' }
 *      ]
 *    }
 *
 *  规则：
 *    · value 由它决定圆半径，也决定离中心的远近（越大越靠里）
 *    · group 就是父子级里的「父体」，同一个 group 才会被合并
 *    · group 省略会自动归到「未分组」
 *    · 也接受简写：[['需求提出', 96, '需求管理'], ...]
 * ============================================================ */

window.RAINDROP_DATA = {
  unit: '次',
  flows: [
    { name: '需求提出', value: 96,  group: '需求管理' },
    { name: '需求评审', value: 74,  group: '需求管理' },
    { name: '方案设计', value: 61,  group: '需求管理' },

    { name: '开发',     value: 128, group: '研发' },
    { name: '工时评估', value: 52,  group: '研发' },
    { name: '单元自测', value: 44,  group: '研发' },
    { name: '排期',     value: 38,  group: '研发' },
    { name: '代码评审', value: 33,  group: '研发' },

    { name: '联调',     value: 27,  group: '发布' },
    { name: '发布',     value: 26,  group: '发布' },
    { name: '提测',     value: 22,  group: '发布' },
    { name: '回归测试', value: 18,  group: '发布' },
    { name: '上线审批', value: 15,  group: '发布' },

    { name: '线上观测', value: 12,  group: '线上运维' },
    { name: '复盘',     value: 9,   group: '线上运维' }
  ]
};

/* ============================================================
 *  点-环模型的数据位：流程树（一个点 = 一个流程，圆周上 = 它的步骤）
 *  ------------------------------------------------------------
 *    { name:'句子', children:[ {name:'主语', children:[...]}, ... ] }
 *    children 的顺序 = 流向顺序（决定圆周上的角度）
 *    叶子可给 value（权重），内部节点权重自动求和
 * ============================================================ */

window.RAINDROP_TREE = {
  name: '这句话',
  children: [
    { name: '主语', children: [
      { name: '这' }, { name: '只' }, { name: '猫' }
    ]},
    { name: '状语', children: [
      { name: '昨天' }, { name: '下午' }
    ]},
    { name: '谓语', children: [
      { name: '睡' }
    ]},
    { name: '处所', children: [
      { name: '在' }, { name: '沙发' }, { name: '上' }
    ]},
    { name: '补语', children: [
      { name: '一整天' }
    ]}
  ]
};

/* 密集版：一个流程下 14 步，权重悬殊 —— 用来验证「周长装不下 / 太小读不出」时的可逆合并 */
window.RAINDROP_TREE_DENSE = {
  name: '订单履约',
  children: [
    { name: '下单', value: 41 }, { name: '支付', value: 38 }, { name: '风控', value: 6 },
    { name: '拆单', value: 5 },  { name: '锁库', value: 9 },  { name: '拣货', value: 7 },
    { name: '复核', value: 4 },  { name: '打包', value: 6 },  { name: '称重', value: 3 },
    { name: '出库', value: 8 },  { name: '干线', value: 7 },  { name: '派送', value: 5 },
    { name: '签收', value: 4 },  { name: '售后', value: 2 }
  ]
};
