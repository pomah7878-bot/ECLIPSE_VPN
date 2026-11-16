(function () {
    'use strict';

    var doc = document.documentElement;
    var reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    var reveals = Array.prototype.slice.call(document.querySelectorAll('.reveal'));
    if (reduced || !('IntersectionObserver' in window)) {
        reveals.forEach(function (el) { el.classList.add('is-in'); });
        doc.classList.add('motion-ready');
    } else {
        var io = new IntersectionObserver(function (entries) {
            entries.forEach(function (entry) {
                if (!entry.isIntersecting) return;
                entry.target.classList.add('is-in');
                io.unobserve(entry.target);
            });
        }, { threshold: 0.12, rootMargin: '0px 0px -6% 0px' });
        reveals.forEach(function (el) { io.observe(el); });

        // Страховка: если observer не сработал, контент не должен остаться скрытым.
        window.setTimeout(function () {
            reveals.forEach(function (el) { el.classList.add('is-in'); });
        }, 2600);
    }

    // Демонстрации приложений запускаются при входе блока в кадр:
    // TV — обход меню пультом, Windows — окно → подключение → настройки.
    var demos = Array.prototype.slice.call(document.querySelectorAll('[data-motion-demo]'));
    if (reduced || !('IntersectionObserver' in window)) {
        demos.forEach(function (el) { el.classList.add('is-visible'); });
    } else if (demos.length) {
        var demoIo = new IntersectionObserver(function (entries) {
            entries.forEach(function (entry) {
                if (!entry.isIntersecting) return;
                entry.target.classList.add('is-visible');
                demoIo.unobserve(entry.target);
            });
        }, { threshold: 0.3, rootMargin: '0px 0px -8% 0px' });
        demos.forEach(function (el) { demoIo.observe(el); });
    }

    if (reduced || !window.matchMedia('(pointer: fine)').matches) {
        doc.classList.add('motion-ready');
        return;
    }

    // Мягкий 3D-наклон карточек устройств.
    document.querySelectorAll('[data-motion-tilt]').forEach(function (el) {
        var rect;
        var reset = function () { el.style.transform = ''; };
        el.addEventListener('pointerenter', function () { rect = el.getBoundingClientRect(); });
        el.addEventListener('pointermove', function (e) {
            if (!rect) rect = el.getBoundingClientRect();
            var x = (e.clientX - rect.left) / rect.width - 0.5;
            var y = (e.clientY - rect.top) / rect.height - 0.5;
            var ry = Math.max(-6, Math.min(6, x * 10));
            var rx = Math.max(-5, Math.min(5, -y * 8));
            el.style.transform = 'perspective(1100px) rotateY(' + ry + 'deg) rotateX(' + rx + 'deg)';
        });
        el.addEventListener('pointerleave', reset);
    });

    // Очень лёгкий параллакс: только transform, без изменения layout.
    var parallax = Array.prototype.slice.call(document.querySelectorAll('[data-motion-parallax]'));
    if (!parallax.length) {
        doc.classList.add('motion-ready');
        return;
    }
    var ticking = false;
    var update = function () {
        var vh = window.innerHeight || 1;
        parallax.forEach(function (el) {
            var r = el.getBoundingClientRect();
            var delta = (r.top + r.height / 2 - vh / 2) / vh;
            var y = Math.max(-14, Math.min(14, delta * -18));
            el.style.transform = 'translate3d(0,' + y.toFixed(2) + 'px,0)';
        });
        ticking = false;
    };
    window.addEventListener('scroll', function () {
        if (!ticking) { window.requestAnimationFrame(update); ticking = true; }
    }, { passive: true });
    update();
    doc.classList.add('motion-ready');
})();
