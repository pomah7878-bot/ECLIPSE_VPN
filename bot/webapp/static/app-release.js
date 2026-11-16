(function () {
    'use strict';

    function formatBytes(bytes) {
        var n = Number(bytes);
        if (!isFinite(n) || n <= 0) return '—';
        return (n / 1048576).toFixed(1).replace('.', ',') + ' МБ';
    }

    function setText(root, selector, value) {
        var el = root.querySelector(selector);
        if (el) el.textContent = value || '—';
    }

    function setSha(root, sha) {
        setText(root, '[data-rel-sha]', sha || '—');
        var copy = root.querySelector('[data-rel-copy]');
        if (copy) copy.disabled = !sha;
    }

    async function headSize(url) {
        try {
            var r = await fetch(url, { method: 'HEAD', cache: 'no-store' });
            if (!r.ok) return 0;
            return Number(r.headers.get('content-length') || 0);
        } catch (e) {
            return 0;
        }
    }

    async function loadAndroid(root) {
        try {
            var rel = await fetch('/api/public/client-update', { cache: 'no-store' }).then(function (r) {
                if (!r.ok) throw new Error('no api');
                return r.json();
            });
            var size = await headSize('/client-update/client.apk');
            setText(root, '[data-rel-version]', 'v' + (rel.version_name || '—') + (rel.version_code ? ' (' + rel.version_code + ')' : ''));
            setText(root, '[data-rel-size]', formatBytes(size));
            setSha(root, rel.sha256 ? String(rel.sha256).toLowerCase() : '');
            root.dataset.loaded = '1';
        } catch (e) {
            root.dataset.error = '1';
            setText(root, '[data-rel-version]', 'нет данных');
        }
    }

    async function loadTv(root) {
        try {
            var rel = await fetch('/api/public/tv-update', { cache: 'no-store' }).then(function (r) {
                if (!r.ok) throw new Error('no api');
                return r.json();
            });
            var size = Number(rel.size || 0) || await headSize('/client-update/tv.apk');
            setText(root, '[data-rel-version]', 'v' + (rel.version_name || '—') + (rel.version_code ? ' (' + rel.version_code + ')' : ''));
            setText(root, '[data-rel-size]', formatBytes(size));
            setSha(root, rel.sha256 ? String(rel.sha256).toLowerCase() : '');
            root.dataset.loaded = '1';
        } catch (e) {
            root.dataset.error = '1';
            setText(root, '[data-rel-version]', 'нет данных');
        }
    }

    async function loadWindows(root) {
        try {
            var rel = await fetch('/api/public/windows-update', { cache: 'no-store' }).then(function (r) {
                if (!r.ok) throw new Error('no windows meta');
                return r.json();
            });
            if (!rel.ok) throw new Error('no windows release');
            setText(root, '[data-rel-version]', rel.tag_name || '—');
            setText(root, '[data-rel-size]', formatBytes(rel.size));
            setSha(root, rel.sha256 ? String(rel.sha256).toLowerCase() : '');
            document.querySelectorAll('[data-download="windows"]').forEach(function (a) {
                if (rel.download_url) a.href = rel.download_url;
            });
            root.dataset.loaded = '1';
        } catch (e) {
            root.dataset.error = '1';
            setText(root, '[data-rel-version]', 'на GitHub');
        }
    }

    function bindCopy() {
        document.querySelectorAll('[data-rel-copy]').forEach(function (btn) {
            btn.addEventListener('click', function () {
                var root = btn.closest('[data-release]');
                if (!root) return;
                var code = root.querySelector('[data-rel-sha]');
                if (!code) return;
                var text = code.textContent.trim();
                if (!text || text === '—') return;
                var done = function () {
                    var old = btn.textContent;
                    btn.textContent = 'Скопировано';
                    setTimeout(function () { btn.textContent = old; }, 1500);
                };
                if (navigator.clipboard && navigator.clipboard.writeText) {
                    navigator.clipboard.writeText(text).then(done).catch(function () {});
                } else {
                    var area = document.createElement('textarea');
                    area.value = text;
                    document.body.appendChild(area);
                    area.select();
                    try { document.execCommand('copy'); done(); } catch (e) {}
                    document.body.removeChild(area);
                }
            });
        });
    }

    function init() {
        bindCopy();
        document.querySelectorAll('[data-release]').forEach(function (root) {
            var kind = root.getAttribute('data-release');
            if (kind === 'android') loadAndroid(root);
            else if (kind === 'tv') loadTv(root);
            else if (kind === 'windows') loadWindows(root);
        });
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();
})();
