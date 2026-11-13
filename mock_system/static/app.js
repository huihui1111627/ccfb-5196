/* Mock 申报系统前端：贸易方式联动字段、申报要素异步加载。
   刻意加入网络延迟，模拟真实系统“联动字段稍后才出现”。 */
(function () {
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  function renderLinked(payload, current) {
    var panel = document.getElementById('linked-panel');
    var box = document.getElementById('linked-fields');
    var label = document.getElementById('linked-mode-label');
    if (!panel || !box) return;
    if (!payload || !payload.fields || !payload.fields.length) { panel.classList.add('hidden'); return; }
    box.innerHTML = '';
    label.textContent = payload.trade_mode + ' ' + payload.label;
    payload.fields.forEach(function (f) {
      var wrap = el('label', 'field');
      var span = el('span', null, f.label + (f.kind === 'select' ? ' *' : ''));
      wrap.appendChild(span);
      var input;
      if (f.kind === 'select') {
        input = el('select');
        var empty = el('option', null, '请选择');
        empty.value = '';
        input.appendChild(empty);
        var opts = f.options || [];
        var labels = f.option_labels || opts;
        opts.forEach(function (o, i) {
          var option = el('option', null, labels[i] || o);
          option.value = o;
          input.appendChild(option);
        });
      } else {
        input = el('input');
        input.type = f.kind === 'number' ? 'number' : 'text';
        if (f.kind === 'number') input.step = '0.0001';
      }
      input.name = f.name;
      input.id = 'linked_' + f.name;
      input.setAttribute('data-linked-field', f.name);
      if (current && current[f.name] !== undefined && current[f.name] !== '') {
        // value is re-selected after async render; used to preserve edits
        setTimeout(function () { input.value = current[f.name]; }, 40);
      }
      wrap.appendChild(input);
      box.appendChild(wrap);
    });
    panel.classList.remove('hidden');
  }

  var modeSelect = document.getElementById('trade_mode');
  if (modeSelect && document.getElementById('header-form')) {
    var currentVals = window.__HEADER_VALUES__ || {};
    function loadLinked() {
      var mode = modeSelect.value;
      var panel = document.getElementById('linked-panel');
      var box = document.getElementById('linked-fields');
      if (!mode) { if (panel) panel.classList.add('hidden'); return; }
      // 延迟 500ms 模拟服务端往返：RPA 必须等待字段出现后再填写
      setTimeout(function () {
        fetch('/api/linked-fields?mode=' + encodeURIComponent(mode), { credentials: 'same-origin' })
          .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
          .then(function (data) {
            renderLinked(data, currentVals);
            var nat = document.getElementById('mode-nature');
            var duty = document.getElementById('mode-duty');
            if (nat) nat.textContent = data.nature || '—';
            if (duty) duty.textContent = data.duty_mode || '—';
            var banner = document.createElement('div');
            banner.id = 'linked-ready';
            banner.hidden = true;
            banner.textContent = data.trade_mode;
          });
      }, 500);
    }
    modeSelect.addEventListener('change', loadLinked);
    if (modeSelect.value) loadLinked();
  }

  window.initItemCards = function () {
    document.querySelectorAll('.item-card').forEach(function (card) {
      var hs = card.querySelector('.hs-code');
      if (!hs) return;
      hs.addEventListener('input', function () {
        var code = (hs.value || '').replace(/\D/g, '');
        if (!code) return;
        var block = card.querySelector('.elements-block');
        var seq = card.getAttribute('data-seq');
        block.innerHTML = '<div class="section-title">申报要素</div><p class="muted small">正在加载税则要素…</p>';
        // 再次加入延迟：要素表异步出现
        setTimeout(function () {
          fetch('/api/elements?hs=' + encodeURIComponent(code), { credentials: 'same-origin' })
            .then(function (r) { if (!r.ok) throw new Error('未找到'); return r.json(); })
            .then(function (data) {
              block.innerHTML = '';
              var title = el('div', 'section-title', '申报要素 — ' + data.name +
                '（法定单位 ' + (data.legal_unit || '') + '）');
              block.appendChild(title);
              var grid = el('div', 'form-grid');
              data.elements.forEach(function (spec) {
                var wrap = el('label', 'field');
                var s = el('span', null, spec.label + (spec.required ? ' *' : ''));
                wrap.appendChild(s);
                var input;
                if (spec.kind === 'select') {
                  input = el('select');
                  var empty = el('option', null, '请选择');
                  empty.value = ''; input.appendChild(empty);
                  spec.options.forEach(function (o) {
                    var opt = el('option', null, o); opt.value = o; input.appendChild(opt);
                  });
                } else {
                  input = el('input');
                  input.type = spec.kind === 'number' ? 'number' : 'text';
                }
                input.name = 'element_' + spec.key;
                input.id = 'element_' + seq + '_' + spec.key;
                input.setAttribute('data-element-key', spec.key);
                wrap.appendChild(input);
                grid.appendChild(wrap);
              });
              block.appendChild(grid);
              var lu = card.querySelector('[name="legal_unit"]');
              var su = card.querySelector('[name="second_unit"]');
              var lq = card.querySelector('[name="legal_quantity"]');
              var sq = card.querySelector('[name="second_quantity"]');
              if (lu && !lu.value) lu.value = data.legal_unit || '';
              if (su && !su.value) su.value = data.second_unit || '';
              if (sq && !data.second_unit) sq.closest('.field').style.display = 'none';
            })
            .catch(function () {
              block.innerHTML = '<div class="section-title">申报要素</div><p class="muted small">未找到该商品编码，请核对 HS 编码</p>';
            });
        }, 400);
      });
    });
  };
})();
