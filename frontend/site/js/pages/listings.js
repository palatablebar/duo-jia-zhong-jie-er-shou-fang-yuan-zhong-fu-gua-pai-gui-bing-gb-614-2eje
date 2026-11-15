import { api } from '../api.js';
import { ENUMS, can, riskTone, statusTone, tagTone, wan } from '../config.js';
import { drawer, errorBox, errorMessage, esc, field, fmtTime, kv, table, toast } from '../ui.js';

const state = { q: '', agencyId: '', status: '', risk: '', propertyId: '' };

function pairCell(raw, norm) {
  return `<div class="pair"><span class="raw">${esc(raw)}</span><span class="arrow">→</span><span class="norm">${esc(norm)}</span></div>`;
}

const CORRECTABLE_FIELDS = [
  ['community_raw', '小区原文'],
  ['layout_raw', '户型原文'],
  ['area_raw', '面积原文'],
  ['price_raw', '价格原文'],
  ['floor_raw', '楼层原文'],
  ['orientation_raw', '朝向原文'],
];

async function loadCorrections(root, item) {
  const node = root.querySelector('#corrections-list');
  if (!node) return;
  try {
    const data = await api.get(`/api/listings/${item.id}/corrections`);
    node.innerHTML = data.items.length
      ? `<table class="data compact"><thead><tr><th>版本</th><th>变更</th><th>来源</th><th>时间</th></tr></thead><tbody>${data.items
          .map(
            (row) => `<tr>
              <td class="mono">v${esc(row.version)}${row.noop ? ' <span class="badge">无变化</span>' : ''}</td>
              <td>${(row.changes || [])
                .map((change) => `<div class="pair"><span class="raw">${esc(change.field)}: ${esc(change.old)}</span><span class="arrow">→</span><span class="norm">${esc(change.new)}</span></div>`)
                .join('') || '<span class="muted">与现值一致</span>'}</td>
              <td>${esc(ENUMS.correctionSource[row.source] || row.source)}<div class="mono muted">${esc(row.correction_key)}</div></td>
              <td class="mono muted">${fmtTime(row.created_at)}</td>
            </tr>`,
          )
          .join('')}</tbody></table>`
      : '<p class="note">还没有修正版本，当前为原始采集版本 v0。</p>';
  } catch (err) {
    node.innerHTML = `<p class="note">${esc(errorMessage(err))}</p>`;
  }
}

function correctionForm(item) {
  const suggested = `corr-${item.agency_code}-${item.lid}-${Date.now().toString(36)}`;
  return `<h3 style="margin:14px 0 8px;font-size:0.95rem">登记上游修正</h3>
    <form id="correction-form" style="display:grid;gap:10px">
      ${field({ label: '修正单号（幂等键，重试不重复入库）', name: 'correction_key', value: suggested, placeholder: '上游修正单号' })}
      ${CORRECTABLE_FIELDS.map(([name, label]) => field({ label, name, value: item[name] || '', placeholder: '不修改则保持原值' })).join('')}
      ${field({ label: '备注', name: 'note', value: '', placeholder: '例如：中介电话确认面积口径为建面' })}
      <button class="btn accent" type="submit">应用修正并重算</button>
      <span class="muted" style="font-size:var(--fs-xs)">提交后：受影响证据失效 → 相关房源簇重算 → 银行清单待重新确认</span>
    </form>`;
}

function openListing(view, ctx, item) {
  const flags = item.parse_flags || [];
  const canCorrect = can(ctx.user.role, 'correction:create');
  const { root, close } = drawer({
    title: `${item.community_raw.trim()} · ${item.layout_raw}`,
    subtitle: `${item.lid} · ${item.agency_name} · ${fmtTime(item.captured_at)}`,
    wide: true,
    body: `${kv([
      ['小区', `${pairCell(item.community_raw, item.norm.community)}`],
      ['户型', `${pairCell(item.layout_raw, item.norm.layout)}`],
      ['面积', `${pairCell(item.area_raw, item.norm.area === null ? '（无法解析）' : `${item.norm.area} ㎡`)}`],
      ['价格', `${pairCell(item.price_raw, item.norm.price ? wan(item.norm.price) : '（无法解析）')}`],
      ['楼层', `${pairCell(item.floor_raw, item.norm.floor ? `${item.norm.floor}/${item.norm.floor_total || '?'} · ${item.norm.floor_band}` : '（仅区间 / 缺失）')}`],
      ['朝向', `${pairCell(item.orientation_raw, item.norm.orientation || '—')}`],
      ['挂牌状态', `<span class="badge ${statusTone(item.status)}">${esc(item.status_label)}</span>`],
      ['挂牌天数 / 带看', `<span class="mono">${esc(item.listed_days)} 天 · ${esc(item.views)} 次</span>`],
      ['所属簇', item.property_id ? `<span class="mono">#${esc(item.property_id)} ${esc(item.property_community || '')} ${esc(item.property_layout || '')}</span>` : '未入簇'],
      ['风险标签', (item.risk_tags || []).length ? item.risk_tags.map((tag) => `<span class="badge ${tagTone(tag)}">${esc(ENUMS.riskTag[tag] || tag)}</span>`).join(' ') : '—'],
      ['当前版本', item.current_correction_id ? `<span class="mono">修正版 #${esc(item.current_correction_id)}</span>` : '原始采集 v0'],
    ])}
    <h3 style="margin:14px 0 8px;font-size:0.95rem">归一标记</h3>
    <div class="chips">${
      flags.length ? flags.map((flag) => `<span class="chip flag">${esc(flag)}</span>`).join('') : '<span class="chip">无（字段规整）</span>'
    }</div>
    <h3 style="margin:14px 0 8px;font-size:0.95rem">标题与切词</h3>
    <p class="note">${esc(item.title)}</p>
    <div class="chips" style="margin-top:8px">${(item.norm.title_tokens || [])
      .slice(0, 18)
      .map((token) => `<span class="chip">${esc(token)}</span>`)
      .join('')}</div>
    <h3 style="margin:14px 0 8px;font-size:0.95rem">修正版本</h3>
    <div id="corrections-list"><span class="muted">读取修正版本…</span></div>
    ${canCorrect ? correctionForm(item) : '<p class="note" style="margin-top:10px">只读角色可查看修正版本，登记修正需要数据专员或管理员。</p>'}`,
    footer: item.property_id
      ? `<button class="btn accent" data-cluster>查看归并簇 #${esc(item.property_id)}</button>`
      : '<span class="mono muted">该挂牌尚未进入任何簇</span>',
  });
  loadCorrections(root, item);
  root.querySelector('[data-cluster]')?.addEventListener('click', () => {
    ctx.navigate(`#/clusters?focus=${item.property_id}`);
  });
  root.querySelector('#correction-form')?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const form = event.target;
    const changes = {};
    CORRECTABLE_FIELDS.forEach(([name]) => {
      const value = form[name].value.trim();
      if (value && value !== (item[name] || '')) changes[name] = value;
    });
    if (!Object.keys(changes).length) {
      toast('没有填写任何变更字段', 'err');
      return;
    }
    try {
      const result = await api.post('/api/corrections', {
        correction_key: form.correction_key.value.trim(),
        agency_code: item.agency_code,
        lid: item.lid,
        changes,
        source: 'manual',
        note: form.note.value.trim() || null,
      });
      toast(result.message || '修正已应用');
      close();
      await listingsPage.render(view, ctx);
    } catch (err) {
      toast(errorMessage(err), 'err');
    }
  });
}

export const listingsPage = {
  async render(view, ctx) {
    if (ctx.query.agency_id) state.agencyId = ctx.query.agency_id;
    if (ctx.query.risk) state.risk = ctx.query.risk;
    if (ctx.query.property_id) state.propertyId = ctx.query.property_id;
    let data;
    let agencies;
    try {
      [data, agencies] = await Promise.all([
        api.get('/api/listings', {
          q: state.q,
          agency_id: state.agencyId,
          status: state.status,
          risk: state.risk,
          property_id: state.propertyId,
          limit: 50,
        }),
        api.get('/api/agencies', { limit: 50 }),
      ]);
    } catch (err) {
      view.innerHTML = errorBox(errorMessage(err));
      view.querySelector('#retry-btn')?.addEventListener('click', () => listingsPage.render(view, ctx));
      return;
    }
    view.innerHTML = `
      <section class="panel">
        <div class="panel-head">
          <h2>挂牌明细 <span class="mono muted">· 原文 vs 归一</span></h2>
          <div style="display:flex;gap:8px;align-items:center">
            <span class="meta">${data.total} 条${state.propertyId ? ` · 簇 #${esc(state.propertyId)}` : ''}</span>
            ${
              state.propertyId
                ? '<button class="btn sm ghost" id="clear-cluster">看全部挂牌</button>'
                : ''
            }
          </div>
        </div>
        <div class="panel-body">
          <div class="filter-bar" id="listing-filters">
            ${field({ label: '搜索', name: 'q', value: state.q, placeholder: '小区 / 编号 / 标题' })}
            ${field({ label: '中介', name: 'agency_id', value: state.agencyId, options: [{ value: '', label: '全部' }, ...agencies.items.map((item) => ({ value: item.id, label: item.name }))] })}
            ${field({ label: '挂牌状态', name: 'status', value: state.status, options: [{ value: '', label: '全部' }, ...Object.entries(ENUMS.listingStatus).map(([value, label]) => ({ value, label }))] })}
            ${field({ label: '风险', name: 'risk', value: state.risk, options: [{ value: '', label: '全部' }, { value: 'bait', label: '钓鱼价' }, { value: 'stale', label: '僵尸挂牌' }, { value: 'none', label: '无标签' }] })}
          </div>
        </div>
        ${table({
          rows: data.items,
          columns: [
            { key: 'lid', label: '挂牌编号', className: 'mono' },
            { key: 'agency_name', label: '中介', render: (row) => esc(row.agency_name) },
            { key: 'community', label: '小区', render: (row) => pairCell(row.community_raw, row.norm.community) },
            { key: 'area', label: '面积', render: (row) => pairCell(row.area_raw, row.norm.area === null ? '—' : `${row.norm.area}㎡`) },
            { key: 'price', label: '价格', render: (row) => pairCell(row.price_raw, row.norm.price ? wan(row.norm.price) : '—') },
            { key: 'floor', label: '楼层', render: (row) => pairCell(row.floor_raw, row.norm.floor ? `${row.norm.floor}/${row.norm.floor_total || '?'} 层` : '仅区间') },
            { key: 'status', label: '状态', render: (row) => `<span class="badge ${statusTone(row.status)}">${esc(row.status_label)}</span>` },
            {
              key: 'risk_tags',
              label: '风险',
              render: (row) =>
                (row.risk_tags || []).length
                  ? row.risk_tags.map((tag) => `<span class="badge ${tagTone(tag)}">${esc(ENUMS.riskTag[tag] || tag)}</span>`).join(' ')
                  : `<span class="muted">${row.property_risk_level ? esc(ENUMS.riskLevel[row.property_risk_level] || '') : '—'}</span>`,
            },
          ],
          empty: { title: '没有匹配的挂牌', hint: '换个中介或风险筛选再看。' },
        })}
      </section>`;

    const filters = view.querySelector('#listing-filters');
    filters?.addEventListener('change', () => {
      state.q = filters.querySelector('[name=q]').value.trim();
      state.agencyId = filters.querySelector('[name=agency_id]').value;
      state.status = filters.querySelector('[name=status]').value;
      state.risk = filters.querySelector('[name=risk]').value;
      state.propertyId = '';
      listingsPage.render(view, ctx);
    });
    filters?.querySelector('[name=q]')?.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        state.q = event.target.value.trim();
        state.propertyId = '';
        listingsPage.render(view, ctx);
      }
    });
    view.querySelector('#clear-cluster')?.addEventListener('click', () => {
      state.propertyId = '';
      listingsPage.render(view, ctx);
    });
    view.querySelectorAll('table.data tbody tr').forEach((row) => {
      row.addEventListener('click', () => {
        const item = data.items.find((entry) => String(entry.id) === row.dataset.id);
        if (item) openListing(view, ctx, item);
      });
    });
  },
};

export default listingsPage;
