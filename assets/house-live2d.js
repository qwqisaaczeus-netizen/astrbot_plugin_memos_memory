(function () {
  "use strict";

  const MODEL_URL = "/assets/live2d/seethrough_output_1/seethrough_output_1.model3.json";
  const CORE_OFFICIAL = "https://cubism.live2d.com/sdk-web/cubismcore/live2dcubismcore.min.js";
  const PIXI_URL = "/assets/vendor/pixi.min.js";
  const DISPLAY_URL = "/assets/vendor/pixi-live2d-display-cubism4.min.js";
  const STATIC_POSES = new Set([
    "tea", "cup", "desk", "book", "reading", "window", "steps",
    "flowers", "yawn", "chest"
  ]);
  const MICRO_PARAMETER_IDS = Object.freeze([
    "ParamArmR", "ParamArmL", "ParamWristR", "ParamWristL",
    "ParamShoulderR", "ParamShoulderL", "ParamTorsoLean", "ParamHemFlutter"
  ]);
  const PHYSICS_PARAMETER_IDS = Object.freeze([
    "ParamSleeveR", "ParamSleeveL", "ParamSkirtSway", "ParamTorsoBreath"
  ]);
  const HEAD_PARAMETER_IDS = Object.freeze([
    "ParamAngleX", "ParamAngleY", "ParamAngleZ", "ParamEyeBallX", "ParamEyeBallY"
  ]);
  // This particular model has a deliberately narrow calibrated head range.
  // Larger pointer-driven angles separate the face, hair and body art meshes.
  const POINTER_LIMITS = Object.freeze({ headX: .62, headY: .38, eyeX: .14, eyeY: .10 });
  const ACTION_PROFILES = Object.freeze({
    nod: { duration: 1250, torso: .12, shoulderR: .05, shoulderL: .05 },
    greet: { duration: 1850, armR: .48, wristR: .28, shoulderR: .15, torso: .08, hem: .08 },
    startle: { duration: 1050, armR: .18, armL: .18, shoulderR: .28, shoulderL: .28, torso: -.20, hem: .18 },
    turn: { duration: 1500, armR: .10, armL: -.08, wristR: .09, wristL: -.06, torso: .15, hem: .14 },
    lean: { duration: 1700, armR: .08, armL: .06, shoulderR: -.05, shoulderL: -.05, torso: .30, hem: .06 },
    sip: { duration: 2200, armR: .52, wristR: .42, shoulderR: .13, torso: .16, hem: .04 },
    pour: { duration: 2250, armR: .38, armL: .12, wristR: .55, wristL: .10, shoulderR: .12, torso: .13, hem: .08 },
    tend: { duration: 2400, armR: .34, armL: .10, wristR: .30, shoulderR: .08, torso: .28, hem: .14 },
    page: { duration: 1850, armR: .20, armL: .16, wristR: .30, wristL: .22, torso: .20, hem: .05 },
    yawn: { duration: 2600, armR: .44, wristR: .38, shoulderR: .18, shoulderL: -.08, torso: -.08, hem: .04 }
  });

  function loadScript(url, id, timeoutMs) {
    const existing = document.getElementById(id);
    if (existing && existing.dataset.loaded === "1") return Promise.resolve();
    return new Promise((resolve, reject) => {
      const script = existing || document.createElement("script");
      const timer = setTimeout(() => reject(new Error("script timeout: " + url)), timeoutMs || 12000);
      script.id = id;
      script.async = true;
      script.onload = () => {
        clearTimeout(timer);
        script.dataset.loaded = "1";
        resolve();
      };
      script.onerror = () => {
        clearTimeout(timer);
        script.remove();
        reject(new Error("script unavailable: " + url));
      };
      if (!existing) {
        script.src = url;
        document.head.appendChild(script);
      }
    });
  }

  async function ensureRuntime() {
    if (!window.Live2DCubismCore) {
      await loadScript(CORE_OFFICIAL, "house-cubism-core-official", 12000);
    }
    if (!window.PIXI) await loadScript(PIXI_URL, "house-pixi", 12000);
    if (!window.PIXI.live2d) await loadScript(DISPLAY_URL, "house-pixi-live2d", 12000);
    if (!window.PIXI || !window.PIXI.live2d || !window.PIXI.live2d.Live2DModel) {
      throw new Error("Live2D browser runtime is incomplete");
    }
  }

  class HouseLive2D {
    constructor(canvas, host) {
      this.canvas = canvas;
      this.host = host;
      this.app = null;
      this.model = null;
      this.loading = null;
      this.active = false;
      this.failed = false;
      this.character = {};
      this.motionConfig = {};
      this.expression = "gentle";
      this.overrideUntil = 0;
      this.startedAt = performance.now();
      this.action = null;
      this.actionStartedAt = 0;
      this.actionDuration = 0;
      this.microValues = Object.create(null);
      this.pointer = { x: 0, y: 0, activeUntil: 0 };
      this.headValues = { x: 0, y: 0, z: 0, eyeX: 0, eyeY: 0 };
      this.reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
      this.resizeObserver = new ResizeObserver(() => this.layout());
      this.resizeObserver.observe(host);
      host.addEventListener("pointermove", event => this.onPointer(event), { passive: true });
      host.addEventListener("pointerleave", () => this.clearPointer(), { passive: true });
    }

    supported(character, motion) {
      const mode = String(motion && motion.character_render_mode || "live2d").toLowerCase();
      return mode !== "classic" && !this.reduced && !STATIC_POSES.has(String(character && character.sprite || ""));
    }

    async init() {
      if (this.model) return true;
      if (this.failed) return false;
      if (this.loading) return this.loading;
      this.loading = (async () => {
        try {
          await ensureRuntime();
          this.app = new PIXI.Application({
            view: this.canvas,
            autoStart: true,
            transparent: true,
            antialias: true,
            resolution: Math.min(2, devicePixelRatio || 1),
            autoDensity: true,
            width: Math.max(2, this.host.clientWidth),
            height: Math.max(2, this.host.clientHeight)
          });
          this.model = await PIXI.live2d.Live2DModel.from(MODEL_URL, {
            autoInteract: false,
            autoUpdate: true
          });
          this.model.anchor.set(0.5, 1);
          this.app.stage.addChild(this.model);
          this.app.ticker.add(() => this.applyFrame());
          this.layout();
          this.host.dataset.live2d = "ready";
          return true;
        } catch (error) {
          this.failed = true;
          this.host.dataset.live2d = "fallback";
          this.host.dataset.live2dError = String(error && error.message || error).slice(0, 180);
          return false;
        }
      })();
      return this.loading;
    }

    async configure(character, motion) {
      this.character = character || {};
      this.motionConfig = motion || {};
      if (!this.supported(this.character, this.motionConfig)) {
        this.setActive(false);
        return false;
      }
      const ready = await this.init();
      this.setActive(ready);
      if (ready) {
        this.setExpression(this.character.expression || "gentle");
        this.layout();
      }
      return ready;
    }

    setActive(value) {
      this.active = Boolean(value && this.model);
      this.host.classList.toggle("live2d-ready", this.active);
      this.canvas.setAttribute("aria-hidden", this.active ? "false" : "true");
      if (this.app) this.app.ticker[this.active ? "start" : "stop"]();
      if (!this.active && this.model) {
        this.clearPointer();
        this.resetHead();
      }
    }

    layout() {
      if (!this.app || !this.model) return;
      const width = Math.max(2, this.host.clientWidth);
      const height = Math.max(2, this.host.clientHeight);
      this.app.renderer.resize(width, height);
      const naturalWidth = Math.max(1, this.model.internalModel.width || this.model.width);
      const naturalHeight = Math.max(1, this.model.internalModel.height || this.model.height);
      const scale = Math.min(width / naturalWidth, height / naturalHeight) * 0.98;
      this.model.scale.set(scale);
      this.model.position.set(width * 0.5, height * 0.985);
    }

    onPointer(event) {
      if (!this.active || !this.model) return;
      if (event.pointerType && event.pointerType !== "mouse" && event.pointerType !== "pen") return;
      const rect = this.host.getBoundingClientRect();
      const x = ((event.clientX - rect.left) / Math.max(1, rect.width) - 0.5) * 2;
      const y = -((event.clientY - rect.top) / Math.max(1, rect.height) - 0.45) * 2;
      this.pointer.x = this.softPointerAxis(x, .16);
      this.pointer.y = this.softPointerAxis(y, .20);
      this.pointer.activeUntil = performance.now() + 720;
    }

    softPointerAxis(value, deadZone) {
      const raw = this.clamp(value, 1);
      const magnitude = Math.abs(raw);
      const edge = Math.max(.01, Math.min(.8, Number(deadZone) || 0));
      if (magnitude <= edge) return 0;
      const normalized = (magnitude - edge) / (1 - edge);
      // Ease the first half of the range so normal cursor movement reads as a
      // glance. The outer edge remains bounded rather than snapping the head.
      return Math.sign(raw) * normalized * normalized;
    }

    clearPointer() {
      this.pointer.x = 0;
      this.pointer.y = 0;
      this.pointer.activeUntil = 0;
    }

    parameter(id, value, weight) {
      const core = this.model && this.model.internalModel && this.model.internalModel.coreModel;
      if (!core) return;
      try { core.setParameterValueById(id, value, weight == null ? 1 : weight); } catch (_) {}
    }

    clamp(value, limit) {
      const edge = Math.max(.01, Number(limit) || 1);
      return Math.max(-edge, Math.min(edge, Number(value) || 0));
    }

    motionStrength() {
      const levels = { gentle: .22, balanced: .32, vivid: .42 };
      const configured = String(this.motionConfig.intensity || "balanced").toLowerCase();
      let strength = levels[configured] || levels.balanced;
      const fatigue = String(this.character && this.character.selection && this.character.selection.fatigue_band || "").toLowerCase();
      if (fatigue === "high" || fatigue === "exhausted") strength *= .72;
      else if (fatigue === "low" || fatigue === "rested") strength *= 1.08;
      if (["sleepy", "melancholy", "wistful"].includes(this.expression)) strength *= .78;
      if (["bright", "playful", "amused"].includes(this.expression)) strength *= 1.10;
      return strength;
    }

    actionValues(now) {
      if (!this.action || !this.actionDuration) return null;
      const elapsed = now - this.actionStartedAt;
      if (elapsed >= this.actionDuration) {
        this.action = null;
        this.actionDuration = 0;
        return null;
      }
      const profile = ACTION_PROFILES[this.action];
      if (!profile) return null;
      const progress = Math.max(0, Math.min(1, elapsed / this.actionDuration));
      // A sine envelope eases both ends and never snaps the body back to rest.
      const envelope = Math.sin(Math.PI * progress);
      return { profile, envelope, progress };
    }

    resetHead() {
      this.headValues = { x: 0, y: 0, z: 0, eyeX: 0, eyeY: 0 };
      HEAD_PARAMETER_IDS.forEach(id => this.parameter(id, 0, 1));
    }

    applyHeadControl(now, t, strength, action) {
      const tracking = !action && now < Number(this.pointer.activeUntil || 0);
      const px = tracking ? this.pointer.x : 0;
      const py = tracking ? this.pointer.y : 0;
      const calm = Math.max(.45, Math.min(1, strength / .32));
      const naturalX = tracking ? 0 : calm * (.48 * Math.sin(t * .29) + .18 * Math.sin(t * .11 + .8));
      const naturalY = tracking ? 0 : calm * .20 * Math.sin(t * .23 + 1.2);
      const naturalZ = tracking ? 0 : calm * .24 * Math.sin(t * .19 + .35);
      let actionX = 0, actionY = 0, actionZ = 0;
      if (action) {
        const e = action.envelope;
        const p = action.progress;
        if (["turn", "pour", "page"].includes(this.action)) actionX = 4.2 * e;
        if (["nod", "greet", "sip", "tend", "yawn", "lean"].includes(this.action)) {
          actionY = 3.4 * Math.sin(Math.PI * 2 * p) * e;
        }
        if (this.action === "startle") actionY = -2.8 * e;
        if (this.action === "lean") actionZ = -1.8 * e;
        if (this.action === "turn") actionZ = -1.2 * e;
      }
      const target = {
        x: this.clamp(naturalX + px * POINTER_LIMITS.headX + actionX, 5),
        y: this.clamp(naturalY + py * POINTER_LIMITS.headY + actionY, 4),
        // Pointer movement must never tilt the head. AngleZ is safe only for
        // the calibrated idle and authored action curves.
        z: this.clamp(naturalZ + actionZ, 2),
        eyeX: this.clamp(px * POINTER_LIMITS.eyeX, POINTER_LIMITS.eyeX),
        eyeY: this.clamp(py * POINTER_LIMITS.eyeY, POINTER_LIMITS.eyeY)
      };
      const smoothing = tracking ? .10 : .075;
      Object.keys(this.headValues).forEach(key => {
        this.headValues[key] += (target[key] - this.headValues[key]) * smoothing;
      });
      this.parameter("ParamAngleX", this.headValues.x, 1);
      this.parameter("ParamAngleY", this.headValues.y, 1);
      this.parameter("ParamAngleZ", this.headValues.z, 1);
      this.parameter("ParamEyeBallX", this.headValues.eyeX, 1);
      this.parameter("ParamEyeBallY", this.headValues.eyeY, 1);
    }

    applyMicroMotion() {
      if (!this.active || !this.model) return;
      const now = performance.now();
      const t = (now - this.startedAt) / 1000;
      const strength = this.motionStrength();
      const fatigue = String(this.character && this.character.selection && this.character.selection.fatigue_band || "").toLowerCase();
      const tempo = fatigue === "high" || fatigue === "exhausted" ? .72 : 1;
      const action = this.actionValues(now);
      const extra = key => action ? (Number(action.profile[key]) || 0) * action.envelope : 0;
      const values = {
        ParamArmR: strength * (.17 * Math.sin(t * .63 * tempo) + .06 * Math.sin(t * .23)) + extra("armR"),
        ParamArmL: strength * (.15 * Math.sin(t * .57 * tempo + 1.75) + .05 * Math.sin(t * .21 + .8)) + extra("armL"),
        ParamWristR: strength * .16 * Math.sin(t * .74 * tempo + .45) + extra("wristR"),
        ParamWristL: strength * .14 * Math.sin(t * .69 * tempo + 2.10) + extra("wristL"),
        ParamShoulderR: strength * .08 * Math.sin(t * .48 * tempo + .2) + extra("shoulderR"),
        ParamShoulderL: strength * .08 * Math.sin(t * .48 * tempo + 1.1) + extra("shoulderL"),
        ParamTorsoLean: strength * (.10 * Math.sin(t * .31 * tempo) + .04 * Math.sin(t * .13)) + extra("torso"),
        ParamHemFlutter: strength * (.15 * Math.sin(t * .41 * tempo + 1.4) + .05 * Math.sin(t * .17)) + extra("hem")
      };
      MICRO_PARAMETER_IDS.forEach(id => {
        const value = this.clamp(values[id], .72);
        this.microValues[id] = value;
        this.parameter(id, value, 1);
      });
      this.applyHeadControl(now, t, strength, action);
    }

    applyFrame() {
      this.applyExpression();
      this.applyMicroMotion();
    }

    applyExpression() {
      if (!this.active || !this.model) return;
      const map = {
        bright: [.55, .35, .18], playful: [.7, .25, .2], amused: [.62, .18, .12],
        surprised: [-.15, .8, .24], sleepy: [-.5, -.2, .08], melancholy: [-.3, -.35, -.08],
        wistful: [-.1, -.2, -.04], focused: [-.28, -.08, -.08], curious: [.1, .18, .08],
        attentive: [.05, .06, .02], serene: [.35, .02, .08], gentle: [.32, .08, .08]
      };
      const values = map[this.expression] || map.gentle;
      this.parameter("ParamEyeLSmile", values[0], .34);
      this.parameter("ParamEyeRSmile", values[0], .34);
      this.parameter("ParamBrowLY", values[1], .30);
      this.parameter("ParamBrowRY", values[1], .30);
      this.parameter("ParamMouthForm", values[2], .36);
      if (performance.now() < this.overrideUntil) return;
    }

    setExpression(value) {
      this.expression = String(value || "gentle").toLowerCase();
      this.applyFrame();
    }

    motion(group) {
      if (!this.active || !this.model || !this.model.motion) return false;
      try {
        const result = this.model.motion(group, 0, 3);
        return result !== false;
      } catch (_) {
        return false;
      }
    }

    react(name) {
      const actionName = String(name || "").toLowerCase();
      const profile = ACTION_PROFILES[actionName];
      if (profile) {
        this.action = actionName;
        this.actionStartedAt = performance.now();
        this.actionDuration = profile.duration;
      }
      const motion = { nod: "Nod", greet: "Nod", startle: "Shake", turn: "Shake", lean: "Nod" }[actionName];
      const played = motion ? this.motion(motion) : false;
      return Boolean(profile || played);
    }

    play(name) {
      const actionName = String(name || "").toLowerCase();
      const motionName = { sip: "Nod", pour: "Shake", tend: "Nod", page: "Shake", yawn: "Nod" }[actionName];
      const profile = ACTION_PROFILES[actionName];
      if (profile) {
        this.action = actionName;
        this.actionStartedAt = performance.now();
        this.actionDuration = profile.duration;
      }
      const played = motionName ? this.motion(motionName) : false;
      if (!profile && !played) return this.react(actionName);
      return Boolean(profile || played);
    }

    forceBlink(duration) {
      if (!this.active || !this.model) return;
      this.overrideUntil = performance.now() + Math.max(120, Number(duration) || 420);
      this.parameter("ParamEyeLOpen", 0, 1);
      this.parameter("ParamEyeROpen", 0, 1);
      setTimeout(() => {
        this.parameter("ParamEyeLOpen", 1, 1);
        this.parameter("ParamEyeROpen", 1, 1);
      }, Math.max(100, Number(duration) || 420));
    }

    debug() {
      return {
        renderer: "cubism-live2d",
        active: this.active,
        loaded: Boolean(this.model),
        failed: this.failed,
        error: this.host.dataset.live2dError || "",
        model: MODEL_URL,
        sprite: this.character.sprite || "",
        micro_motion: {
          enabled: this.active,
          action: this.action || "idle",
          driven_parameters: MICRO_PARAMETER_IDS.slice(),
          head_parameters: HEAD_PARAMETER_IDS.slice(),
          physics_parameters: PHYSICS_PARAMETER_IDS.slice(),
          values: Object.assign({}, this.microValues),
          head_values: Object.assign({}, this.headValues),
          pointer_tracking: performance.now() < Number(this.pointer.activeUntil || 0),
          pointer_limits: Object.assign({}, POINTER_LIMITS)
        }
      };
    }

    destroy() {
      this.resizeObserver.disconnect();
      if (this.app) this.app.destroy(false, { children: true, texture: false, baseTexture: false });
      this.app = null;
      this.model = null;
    }
  }

  window.HouseLive2D = HouseLive2D;
})();
