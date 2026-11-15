import { api } from '../api.js';
import { ENUMS, can, confirmationTone, wan } from '../config.js';
import { badge, drawer, errorBox, errorMessage, esc, field, fmtTime, kv, table, toast } from '../ui.js';

const state = { q: '', confirmation: '' };

function evidenceCell(item, fieldKey) {
  const entry = (item.evidence || {})[fieldKey] || {};
  if (!entry.complete) return '<span class="badge danger">缺</span>';
  const first = (entry.supporters || [])[0] || {};
  const more = entry.supporters.length > 1 ? ` 等${entry.supporters.length}条` : '';
  return `<span class="badge ok">齐</span> <span class="mono muted">${esc(first.agency_name || '')}《${esc(first.source_text || '')}》${esc(more)}</span>`;
}

function evidenceBlock(item) {
  return ['area', 'price', 'layout']
    .map((key) => {
      const entry = (item.evidence || {})[key] || { supporters: [] };
      const rows = (entry.supporters || [])
        .map(
          (sup) => `<tr>
            <td>${esc(sup.agency_name)}<div class="mono muted">${esc(sup.lid || '')}</div></td>
            <td class="mono">${esc(sup.source_text)}</td>
            <td>${esc(ENUMS.evidenceRule[sup.rule] || sup.rule)}</td>
          </tr>`,
        )
        .join('');
      return `<h3 style="margin:14px 0 8px;font-size:0.95rem">${esc(ENUMS.evidenceField[key])}证据
          <span class="mono muted">采信值 ${esc(entry.value || '—')}</span></h3>
        ${rows ? `<table class="data compact"><thead><tr><th>中介 / 编号</th><th>采集原文</th><th>采信规则</th></tr></thead><tbody>${rows}</tbody></table>` : '<p class="note">该字段暂无有效证据。</p>'}`;
    })
    .join('');
}

async function openItem(view, ctx, item) {
  const canConfirm = can(ctx.user.role, 'property:confirm');
  const { root, close } = drawer({
    title: `${item.community} · ${item.layout}`,
    subtitle: `簇 #${item.property_id} · 主档 revision ${item.revision} · ${item.confirmation_state_label}`,
    wide: true,
    body: `${kv([
      ['面积众数 / 价格中位', `<span class="mono">${esc(item.area_mode)} ㎡ · ${esc(wan(item.price_median))}</span>`],
      ['成员 / 中介', `${esc(item.member_count)} 条 / ${esc(item.agency_count)} 家`],
      ['确认状态', badge(confirmationTone(item.confirmation_state), item.confirmation_state_label)],
      ['确认人 / 时间', item.confirmed_by ? `${esc(item.confirmed_by)} · ${fmtTime(item.confirmed_at)}` : '—'],
      ['最近受影响', item.last_affected_at ? fmtTime(item.last_affected_at) : '未被修正波及'],
    ])}
    ${evidenceBlock(item)}
    <p class="note">银行清单只列当前有效且证据齐全的房源；标「受影响后重新确认」表示该房源曾被上游修正波及、估价师已在重算后再次确认。</p>`,
    footer: canConfirm
      ? `<form id="confirm-form" style="display:grid;gap:10px">
          ${field({ label: '确认备注', name: 'note', value: '', placeholder: '例如：已核对 4 家中介报价与产证面积' })}
          <button class="btn accent" type="submit">${item.confirmation_state === 'pending' ? '确认当前主档' : '再次确认'}</button>
        </form>`
      : '<span class="mono muted">只读角色可以查看清单，确认需要数据专员或管理员</span>',
  });
  root.querySelector('#confirm-form')?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const form = event.target;
    try {
      const result = await api.post(`/api/properties/${item.property_id}/confirm`, { note: form.note.value.trim() || null });
      toast(result.reconfirmed ? '已完成受影响后重新确认' : '主档已确认');
      close();
      await bankPage.render(view, ctx);
    } catch (err) {
      toast(errorMessage(err), 'err');
    }
  });
}

export const bankPage = {
  async render(view, ctx) {
    let data;
    try {
      data = await api.get('/api/bank-list', { q: state.q, confirmation: state.confirmation, limit: 100 });
    } catch (err) {
      view.innerHTML = errorBox(errorMessage(err));
      view.querySelector('#retry-btn')?.addEventListener('click', () => bankPage.render(view, ctx));
      return;
    }
    const reconfirmedCount = data.items.filter((item) => item.reconfirmed).length;
    view.innerHTML = `
      <section class="panel">
        <div class="panel-head">
          <h2>银行清单 <span class="mono muted">· 当前有效且证据齐全</span></h2>
          <div style="display:flex;gap:8px;align-items:center">
            <span class="meta">${data.items.length} 套 · 受影响后重新确认 ${reconfirmedCount} 套</span>
            ${can(ctx.user.role, 'export:read') ? '<button class="btn sm ghost accent" id="export-bank">导出银行清单 CSV</button>' : ''}
          </div>
        </div>
        <div class="panel-body">
          <div class="filter-bar" id="bank-filters">
            ${field({ label: '搜索', name: 'q', value: state.q, placeholder: '小区 / 户型' })}
            ${field({
              label: '确认状态',
              name: 'confirmation',
              value: state.confirmation,
              options: [{ value: '', label: '全部' }, ...Object.entries(ENUMS.confirmationState).map(([value, label]) => ({ value, label }))],
            })}
          </div>
        </div>
        ${table({
          rows: data.items.map((item) => ({ ...item, id: item.property_id })),
          columns: [
            { key: 'community', label: '小区 / 户型', render: (row) => `<b>${esc(row.community)}</b><div class="mono muted">${esc(row.layout)} · 簇 #${esc(row.property_id)}</div>` },
            { key: 'area_mode', label: '面积', className: 'num', render: (row) => `${esc(row.area_mode)} ㎡` },
            { key: 'price_median', label: '价格中位', className: 'num', render: (row) => esc(wan(row.price_median)) },
            { key: 'agency_count', label: '中介 / 成员', className: 'num', render: (row) => `${esc(row.agency_count)} / ${esc(row.member_count)}` },
            { key: 'evidence', label: '面积证据', render: (row) => evidenceCell(row, 'area') },
            { key: 'evidence2', label: '价格证据', render: (row) => evidenceCell(row, 'price') },
            { key: 'evidence3', label: '户型证据', render: (row) => evidenceCell(row, 'layout') },
            {
              key: 'confirmation_state',
              label: '确认状态',
              render: (row) =>
                `${badge(confirmationTone(row.confirmation_state), row.confirmation_state_label)}${
                  row.reconfirmed ? ' <span class="badge info">重新确认</span>' : ''
                }`,
            },
          ],
          empty: { title: '清单为空', hint: '还没有当前有效且证据齐全的房源，先跑一次归并重算。' },
        })}
      </section>`;

    const filters = view.querySelector('#bank-filters');
    filters?.addEventListener('change', () => {
      state.q = filters.querySelector('[name=q]').value.trim();
      state.confirmation = filters.querySelector('[name=confirmation]').value;
      bankPage.render(view, ctx);
    });
    filters?.querySelector('[name=q]')?.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        state.q = event.target.value.trim();
        bankPage.render(view, ctx);
      }
    });
    view.querySelectorAll('table.data tbody tr').forEach((row) => {
      row.addEventListener('click', () => {
        const item = data.items.find((entry) => String(entry.property_id) === row.dataset.id);
        if (item) openItem(view, ctx, item);
      });
    });
    view.querySelector('#export-bank')?.addEventListener('click', async () => {
      try {
        await api.download('/api/exports/bank-list.csv', 'bank-list.csv');
        toast('已开始下载银行清单');
      } catch (err) {
        toast(errorMessage(err), 'err');
      }
    });
  },
};

export default bankPage;
