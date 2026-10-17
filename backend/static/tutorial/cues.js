/* One delayed arrow per tutorial surface. No polling, network, or live UI access. */
(() => {
  'use strict';
  window.FuraticTutorialCue = class {
    constructor(root) {
      this.target = null;
      this.timer = 0;
      this.frame = 0;
      this.armed = false;
      this.arrow = document.createElement('div');
      this.arrow.className = 'ft-idle-arrow';
      this.arrow.setAttribute('aria-hidden', 'true');
      this.arrow.innerHTML = '<span class="ft-arrow-visual"><svg viewBox="0 0 36 36"><path d="M4 18h26m-9-9 9 9-9 9"/></svg></span>';
      root.append(this.arrow);
      this.reposition = () => {
        if (!this.armed || this.frame) return;
        this.frame = requestAnimationFrame(() => { this.frame = 0; this.position(); });
      };
      this.visibility = () => {
        const target = this.target;
        this.pointTo(null);
        if (!document.hidden) this.pointTo(target);
        else this.target = target;
      };
      this.resize = typeof ResizeObserver === 'function' ? new ResizeObserver(this.reposition) : null;
      window.addEventListener('resize', this.reposition);
      document.addEventListener('scroll', this.reposition, true);
      document.addEventListener('visibilitychange', this.visibility);
      this.onPageHide = () => this.destroy();
      window.addEventListener('pagehide', this.onPageHide, {once: true});
    }
    pointTo(target) {
      if (target === this.target) return;
      clearTimeout(this.timer); this.timer = 0;
      cancelAnimationFrame(this.frame); this.frame = 0;
      this.resize?.disconnect();
      this.armed = false;
      this.arrow.classList.remove('is-visible');
      this.target = target;
      if (!target || document.hidden) return;
      this.resize?.observe(target);
      this.timer = setTimeout(() => {
        this.timer = 0;
        if (!this.target?.isConnected || document.hidden) return;
        this.armed = true;
        this.position();
      }, 5000);
    }
    position() {
      if (!this.target?.isConnected) { this.pointTo(null); return false; }
      const rect = this.target.getBoundingClientRect();
      const width = document.documentElement.clientWidth || innerWidth;
      const height = document.documentElement.clientHeight || innerHeight;
      if (!rect.width || !rect.height || rect.bottom < 0 || rect.top > height || rect.right < 0 || rect.left > width) {
        this.arrow.classList.remove('is-visible'); return false;
      }
      const size = this.arrow.offsetWidth || 45;
      const half = size / 2, gap = 16, edge = 12;
      const room = size + gap + edge;
      let x, y, angle;
      if (rect.left >= room) { x = rect.left - size - gap; y = rect.top + rect.height / 2 - half; angle = 0; }
      else if (width - rect.right >= room) { x = rect.right + gap; y = rect.top + rect.height / 2 - half; angle = 180; }
      else if (height - rect.bottom >= room) { x = rect.left + rect.width / 2 - half; y = rect.bottom + gap; angle = -90; }
      else { x = rect.left + rect.width / 2 - half; y = rect.top - size - gap; angle = 90; }
      Object.assign(this.arrow.style, {left: Math.max(edge, Math.min(width - size - edge, x)) + 'px', top: Math.max(edge, Math.min(height - size - edge, y)) + 'px', transform: 'rotate(' + angle + 'deg)'});
      this.arrow.classList.add('is-visible');
      return true;
    }
    destroy() {
      this.pointTo(null);
      this.arrow.remove();
      window.removeEventListener('resize', this.reposition);
      document.removeEventListener('scroll', this.reposition, true);
      document.removeEventListener('visibilitychange', this.visibility);
      window.removeEventListener('pagehide', this.onPageHide);
    }
  };
})();
