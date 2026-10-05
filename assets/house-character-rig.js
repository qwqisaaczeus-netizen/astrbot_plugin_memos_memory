(function () {
  "use strict";

  const STANDING = {
    head: [0.50, 0.16, 0.16, 0.18], hair: [0.50, 0.35, 0.25, 0.40],
    torso: [0.50, 0.42, 0.23, 0.25], leftSleeve: [0.32, 0.49, 0.20, 0.28],
    rightSleeve: [0.68, 0.49, 0.20, 0.28], skirt: [0.50, 0.74, 0.37, 0.30],
    prop: [0.50, 0.42, 0.12, 0.16], phase: 0.0,
  };
  const SEATED = {
    head: [0.50, 0.18, 0.17, 0.19], hair: [0.51, 0.37, 0.27, 0.39],
    torso: [0.50, 0.43, 0.24, 0.24], leftSleeve: [0.32, 0.49, 0.22, 0.27],
    rightSleeve: [0.68, 0.49, 0.22, 0.27], skirt: [0.50, 0.72, 0.42, 0.31],
    prop: [0.50, 0.50, 0.14, 0.14], phase: 0.0,
  };

  function profile(base, overrides) {
    return Object.assign({}, base, overrides || {});
  }

  const RIGS = {
    fan: profile(STANDING, {prop: [0.36, 0.36, 0.15, 0.22], phase: 0.1}),
    letter: profile(STANDING, {head: [0.50, 0.15, 0.16, 0.18], prop: [0.50, 0.34, 0.22, 0.15], phase: 0.8}),
    tea: profile(SEATED, {head: [0.51, 0.17, 0.17, 0.18], prop: [0.26, 0.62, 0.14, 0.12], phase: 1.5}),
    desk: profile(SEATED, {head: [0.52, 0.18, 0.17, 0.18], prop: [0.49, 0.56, 0.23, 0.16], phase: 2.1}),
    window: profile(SEATED, {head: [0.48, 0.16, 0.17, 0.18], hair: [0.46, 0.36, 0.28, 0.40], skirt: [0.46, 0.68, 0.43, 0.30], phase: 2.7}),
    chest: profile(SEATED, {head: [0.55, 0.15, 0.17, 0.18], torso: [0.56, 0.40, 0.23, 0.24], prop: [0.46, 0.35, 0.22, 0.15], phase: 3.2}),
    playful: profile(STANDING, {head: [0.50, 0.17, 0.16, 0.18], torso: [0.50, 0.43, 0.22, 0.25], phase: 3.8}),
    reading: profile(SEATED, {head: [0.54, 0.17, 0.17, 0.18], prop: [0.49, 0.47, 0.21, 0.14], phase: 4.3}),
    lantern: profile(STANDING, {head: [0.50, 0.14, 0.15, 0.17], prop: [0.29, 0.38, 0.15, 0.25], phase: 4.9}),
    wistful: profile(STANDING, {head: [0.53, 0.14, 0.15, 0.17], hair: [0.54, 0.35, 0.24, 0.41], phase: 5.5}),
    flowers: profile(SEATED, {head: [0.48, 0.20, 0.17, 0.18], torso: [0.48, 0.45, 0.24, 0.24], prop: [0.34, 0.76, 0.18, 0.13], phase: 6.1}),
    steps: profile(SEATED, {head: [0.51, 0.16, 0.16, 0.18], prop: [0.42, 0.42, 0.13, 0.18], phase: 6.7}),
    yawn: profile(SEATED, {head: [0.50, 0.16, 0.17, 0.18], leftSleeve: [0.37, 0.43, 0.19, 0.25], phase: 7.2}),
    lean: profile(STANDING, {head: [0.51, 0.15, 0.17, 0.18], torso: [0.51, 0.39, 0.23, 0.24], hair: [0.53, 0.34, 0.26, 0.40], phase: 7.8}),
    book: profile(SEATED, {head: [0.48, 0.18, 0.17, 0.18], prop: [0.42, 0.49, 0.22, 0.17], phase: 8.4}),
    cup: profile(SEATED, {head: [0.50, 0.17, 0.17, 0.18], prop: [0.50, 0.43, 0.14, 0.13], phase: 9.0}),
    "court-smile": profile(STANDING, {head: [0.50, 0.15, 0.14, 0.16], hair: [0.50, 0.35, 0.21, 0.40], torso: [0.50, 0.41, 0.18, 0.24], leftSleeve: [0.42, 0.48, 0.14, 0.24], rightSleeve: [0.59, 0.48, 0.14, 0.24], skirt: [0.50, 0.72, 0.24, 0.32], prop: [0.38, 0.36, 0.10, 0.16], phase: 9.6}),
    "court-surprised": profile(STANDING, {head: [0.50, 0.15, 0.14, 0.16], hair: [0.50, 0.35, 0.21, 0.40], torso: [0.50, 0.41, 0.18, 0.24], leftSleeve: [0.42, 0.48, 0.14, 0.24], rightSleeve: [0.59, 0.48, 0.14, 0.24], skirt: [0.50, 0.72, 0.24, 0.32], prop: [0.38, 0.36, 0.10, 0.16], phase: 10.2}),
    "court-soft": profile(STANDING, {head: [0.50, 0.15, 0.14, 0.16], hair: [0.50, 0.35, 0.21, 0.40], torso: [0.50, 0.41, 0.18, 0.24], leftSleeve: [0.42, 0.48, 0.14, 0.24], rightSleeve: [0.59, 0.48, 0.14, 0.24], skirt: [0.50, 0.72, 0.24, 0.32], prop: [0.38, 0.36, 0.10, 0.16], phase: 10.8}),
  };

  const FACE_OVERRIDES = {
    fan: [0.50, 0.155, 0.088, 0.070], letter: [0.50, 0.145, 0.086, 0.070],
    tea: [0.51, 0.165, 0.088, 0.070], desk: [0.52, 0.175, 0.086, 0.070],
    window: [0.48, 0.155, 0.086, 0.070], chest: [0.55, 0.145, 0.086, 0.070],
    playful: [0.50, 0.165, 0.086, 0.070], reading: [0.54, 0.165, 0.086, 0.070],
    lantern: [0.50, 0.135, 0.082, 0.066], wistful: [0.53, 0.135, 0.082, 0.066],
    flowers: [0.48, 0.195, 0.086, 0.070], steps: [0.51, 0.155, 0.086, 0.070],
    yawn: [0.50, 0.155, 0.086, 0.070], lean: [0.51, 0.145, 0.088, 0.070],
    book: [0.48, 0.175, 0.086, 0.070], cup: [0.50, 0.165, 0.086, 0.070],
    "court-smile": [0.50, 0.145, 0.064, 0.050],
    "court-surprised": [0.50, 0.145, 0.064, 0.050],
    "court-soft": [0.50, 0.145, 0.064, 0.050],
  };
  Object.entries(RIGS).forEach(([name, rig]) => {
    rig.face = FACE_OVERRIDES[name] || [rig.head[0], rig.head[1], 0.084, 0.068];
  });

  const FACE_EXPRESSIONS = {
    gentle: {lid: 0.04, smile: 0.30, mouth: 0.04, gazeY: 0.00},
    bright: {lid: 0.00, smile: 0.70, mouth: 0.10, gazeY: -0.02},
    surprised: {lid: -0.18, smile: 0.02, mouth: 0.38, gazeY: -0.03},
    playful: {lid: 0.05, smile: 0.78, mouth: 0.12, gazeY: 0.00},
    sleepy: {lid: 0.40, smile: 0.04, mouth: 0.03, gazeY: 0.12},
    melancholy: {lid: 0.20, smile: -0.18, mouth: 0.02, gazeY: 0.14},
    wistful: {lid: 0.16, smile: -0.06, mouth: 0.02, gazeY: 0.09},
    curious: {lid: -0.08, smile: 0.18, mouth: 0.12, gazeY: -0.02},
    focused: {lid: 0.12, smile: 0.00, mouth: 0.00, gazeY: 0.08},
    absorbed: {lid: 0.16, smile: 0.02, mouth: 0.00, gazeY: 0.12},
    serene: {lid: 0.12, smile: 0.38, mouth: 0.02, gazeY: 0.06},
    amused: {lid: 0.10, smile: 0.74, mouth: 0.12, gazeY: 0.00},
    attentive: {lid: -0.04, smile: 0.10, mouth: 0.04, gazeY: -0.02},
    pensive: {lid: 0.16, smile: -0.02, mouth: 0.00, gazeY: 0.11},
  };
  const FACE_ALIASES = {
    "温柔浅笑": "gentle", "浅笑": "gentle", "温柔": "gentle", "明亮俏皮": "playful",
    "亲昵含笑": "playful", "微微惊讶": "surprised", "困倦": "sleepy", "睡意未消": "sleepy",
    "低落": "melancholy", "若有所思": "wistful", "好奇": "curious", "专注": "focused",
    "沉浸": "absorbed", "安宁": "serene", "含笑": "amused", "专注回望": "attentive", "沉思": "pensive",
  };

  const VERTEX_SHADER = `
    attribute vec2 aPosition;
    attribute vec2 aUv;
    uniform vec4 uHead;
    uniform vec4 uHair;
    uniform vec4 uTorso;
    uniform vec4 uLeftSleeve;
    uniform vec4 uRightSleeve;
    uniform vec4 uSkirt;
    uniform vec4 uProp;
    uniform vec4 uOffsets0;
    uniform vec4 uOffsets1;
    uniform vec4 uOffsets2;
    uniform vec4 uOffsets3;
    uniform mediump vec4 uCell;
    uniform mediump vec4 uFace;
    uniform mediump vec4 uFaceMotion;
    varying vec2 vUv;
    varying vec2 vLocalUv;

    float weightAt(vec2 point, vec4 region) {
      vec2 safeSize = max(region.zw, vec2(0.02));
      vec2 q = (point - region.xy) / safeSize;
      return exp(-2.25 * dot(q, q));
    }

    void main() {
      float head = weightAt(aUv, uHead);
      float hair = weightAt(aUv, uHair) * smoothstep(0.08, 0.70, aUv.y);
      float torso = weightAt(aUv, uTorso);
      float leftSleeve = weightAt(aUv, uLeftSleeve);
      float rightSleeve = weightAt(aUv, uRightSleeve);
      float skirt = weightAt(aUv, uSkirt) * smoothstep(0.46, 0.94, aUv.y);
      float prop = weightAt(aUv, uProp);

      vec2 displacement = vec2(0.0);
      displacement += head * uOffsets0.xy;
      displacement += hair * uOffsets0.zw;
      displacement += torso * uOffsets1.xy;
      displacement += leftSleeve * uOffsets1.zw;
      displacement += rightSleeve * uOffsets2.xy;
      displacement += skirt * uOffsets2.zw;
      displacement += prop * uOffsets3.xy;

      vec2 headVector = aUv - uHead.xy;
      float angle = uOffsets3.z * head;
      vec2 rotated = vec2(
        headVector.x * cos(angle) - headVector.y * sin(angle),
        headVector.x * sin(angle) + headVector.y * cos(angle)
      );
      displacement += (rotated - headVector) * head;
      displacement.x += (aUv.x - uTorso.x) * torso * uOffsets3.w;
      float faceDepth = weightAt(aUv, uFace);
      displacement.x += (aUv.x - uFace.x) * faceDepth * uFaceMotion.y * 0.055;
      displacement.y += (aUv.y - uFace.y) * faceDepth * uFaceMotion.z * 0.025;

      float edge = smoothstep(0.0, 0.035, aUv.x)
        * smoothstep(0.0, 0.035, 1.0 - aUv.x)
        * smoothstep(0.0, 0.035, aUv.y)
        * smoothstep(0.0, 0.035, 1.0 - aUv.y);
      displacement *= edge;

      vec2 clip = aPosition + vec2(displacement.x * 2.0, -displacement.y * 2.0);
      gl_Position = vec4(clip, 0.0, 1.0);
      vUv = uCell.xy + aUv * uCell.zw;
      vLocalUv = aUv;
    }
  `;

  const FRAGMENT_SHADER = `
    precision mediump float;
    uniform sampler2D uTexture;
    uniform mediump vec4 uCell;
    uniform mediump vec4 uFace;
    uniform mediump vec4 uFaceMotion;
    uniform vec4 uFaceStyle;
    varying vec2 vUv;
    varying vec2 vLocalUv;

    float ellipse(vec2 point, vec2 center, vec2 radius) {
      vec2 q = (point - center) / max(radius, vec2(0.0005));
      return exp(-4.8 * dot(q, q));
    }

    void main() {
      vec2 local = vLocalUv;
      float eyeY = uFace.y - uFace.w * 0.11;
      vec2 leftEye = vec2(uFace.x - uFace.z * 0.24, eyeY);
      vec2 rightEye = vec2(uFace.x + uFace.z * 0.24, eyeY);
      vec2 eyeRadius = vec2(uFace.z * 0.20, uFace.w * 0.075);
      float leftMask = ellipse(local, leftEye, eyeRadius);
      float rightMask = ellipse(local, rightEye, eyeRadius);
      float eyeMask = max(leftMask, rightMask);
      float blink = clamp(uFaceMotion.x + uFaceStyle.x, 0.0, 1.0);
      local.x -= uFaceMotion.y * uFace.z * 0.055 * eyeMask;
      local.y -= uFaceMotion.z * uFace.w * 0.035 * eyeMask;
      local.y = mix(local.y, eyeY, blink * eyeMask * 0.70);

      float mouthY = uFace.y + uFace.w * 0.34;
      vec2 mouthCenter = vec2(uFace.x, mouthY);
      float mouthArea = ellipse(local, mouthCenter, vec2(uFace.z * 0.28, uFace.w * 0.16));
      float mouthCurve = (local.x - uFace.x) * (local.x - uFace.x) / max(0.0001, uFace.z * uFace.z);
      local.y -= uFaceStyle.y * uFace.w * (0.20 - mouthCurve * 0.16) * mouthArea;
      local.y += uFaceStyle.z * uFace.w * 0.055 * mouthArea;

      vec2 sampleUv = uCell.xy + local * uCell.zw;
      vec4 color = texture2D(uTexture, sampleUv);
      if (color.a < 0.002) discard;
      float lidLine = max(
        ellipse(vLocalUv, leftEye, vec2(uFace.z * 0.18, uFace.w * 0.018)),
        ellipse(vLocalUv, rightEye, vec2(uFace.z * 0.18, uFace.w * 0.018))
      ) * smoothstep(0.42, 0.94, blink);
      float smileLineY = mouthY + uFaceStyle.y * uFace.w * (0.055 - mouthCurve * 0.10);
      float mouthLine = ellipse(
        vLocalUv,
        vec2(uFace.x, smileLineY),
        vec2(uFace.z * 0.22, uFace.w * (0.018 + uFaceStyle.z * 0.030))
      ) * clamp(abs(uFaceStyle.y) * 0.75 + uFaceStyle.z, 0.0, 1.0);
      color.rgb = mix(color.rgb, vec3(0.20, 0.105, 0.10), clamp(lidLine * 0.20 + mouthLine * 0.16, 0.0, 0.28));
      gl_FragColor = color;
    }
  `;

  function compile(gl, type, source) {
    const shader = gl.createShader(type);
    gl.shaderSource(shader, source);
    gl.compileShader(shader);
    if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
      const message = gl.getShaderInfoLog(shader) || "shader compile failed";
      gl.deleteShader(shader);
      throw new Error(message);
    }
    return shader;
  }

  function createProgram(gl) {
    const program = gl.createProgram();
    gl.attachShader(program, compile(gl, gl.VERTEX_SHADER, VERTEX_SHADER));
    gl.attachShader(program, compile(gl, gl.FRAGMENT_SHADER, FRAGMENT_SHADER));
    gl.linkProgram(program);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
      throw new Error(gl.getProgramInfoLog(program) || "shader link failed");
    }
    return program;
  }

  function createMesh(gl, columns, rows) {
    const vertices = [];
    const indices = [];
    for (let y = 0; y <= rows; y += 1) {
      const v = y / rows;
      for (let x = 0; x <= columns; x += 1) {
        const u = x / columns;
        vertices.push(u * 2 - 1, 1 - v * 2, u, v);
      }
    }
    for (let y = 0; y < rows; y += 1) {
      for (let x = 0; x < columns; x += 1) {
        const a = y * (columns + 1) + x;
        const b = a + 1;
        const c = a + columns + 1;
        const d = c + 1;
        indices.push(a, c, b, b, c, d);
      }
    }
    return {
      vertices: new Float32Array(vertices),
      indices: new Uint16Array(indices),
    };
  }

  function intensityValue(name) {
    return {gentle: 0.78, balanced: 1.42, vivid: 1.92}[name] || 1.42;
  }

  function easePulse(value) {
    if (value <= 0 || value >= 1) return 0;
    return Math.sin(Math.PI * value);
  }

  function prepareAtlas(image, atlas) {
    const canvas = document.createElement("canvas");
    canvas.width = image.naturalWidth;
    canvas.height = image.naturalHeight;
    const context = canvas.getContext("2d", {willReadFrequently: true});
    context.clearRect(0, 0, canvas.width, canvas.height);
    context.drawImage(image, 0, 0);
    const frame = context.getImageData(0, 0, canvas.width, canvas.height);
    const pixels = frame.data;
    const source = new Uint8ClampedArray(pixels);
    const width = canvas.width;
    const height = canvas.height;
    const index = (x, y) => (y * width + x) * 4;
    const alpha = (x, y) => source[index(x, y) + 3];
    const luminance = (offset) => source[offset] * 0.2126 + source[offset + 1] * 0.7152 + source[offset + 2] * 0.0722;
    const boundary = [];
    const distance = new Uint8Array(width * height);
    const queue = new Int32Array(width * height);
    let queueHead = 0;
    let queueTail = 0;
    const maxDepth = atlas === "a" || atlas === "b" ? 8 : 2;

    if (atlas === "a" || atlas === "b") {
      const candidates = new Uint8Array(width * height);
      for (let y = 1; y < height - 1; y += 1) {
        for (let x = 1; x < width - 1; x += 1) {
          const offset = index(x, y);
          const high = Math.max(source[offset], source[offset + 1], source[offset + 2]);
          const low = Math.min(source[offset], source[offset + 1], source[offset + 2]);
          if (source[offset + 3] > 3 && luminance(offset) > 182 && high - low < 28) candidates[y * width + x] = 1;
        }
      }
      const stack = [];
      for (let position = 0; position < candidates.length; position += 1) {
        if (!candidates[position]) continue;
        candidates[position] = 0;
        stack.push(position);
        const component = [];
        let touchesTransparent = false;
        let minX = width;
        let minY = height;
        let maxX = 0;
        let maxY = 0;
        while (stack.length) {
          const current = stack.pop();
          component.push(current);
          const x = current % width;
          const y = Math.floor(current / width);
          minX = Math.min(minX, x);
          minY = Math.min(minY, y);
          maxX = Math.max(maxX, x);
          maxY = Math.max(maxY, y);
          for (const next of [current - 1, current + 1, current - width, current + width]) {
            if (source[next * 4 + 3] <= 3) touchesTransparent = true;
            if (candidates[next]) {
              candidates[next] = 0;
              stack.push(next);
            }
          }
        }
        if (!touchesTransparent || component.length > 180 || maxX - minX > 32 || maxY - minY > 32) continue;
        for (const current of component) {
          source[current * 4 + 3] = 0;
          pixels[current * 4 + 3] = 0;
        }
      }
    }

    for (let y = 1; y < height - 1; y += 1) {
      for (let x = 1; x < width - 1; x += 1) {
        const offset = index(x, y);
        const a = source[offset + 3];
        if (a <= 3) continue;
        if (alpha(x - 1, y) <= 3 || alpha(x + 1, y) <= 3 || alpha(x, y - 1) <= 3 || alpha(x, y + 1) <= 3) {
          const position = y * width + x;
          distance[position] = 1;
          queue[queueTail++] = position;
        }
      }
    }

    while (queueHead < queueTail) {
      const position = queue[queueHead++];
      const x = position % width;
      const y = Math.floor(position / width);
      const depth = distance[position];
      boundary.push([x, y, index(x, y), depth]);
      if (depth >= maxDepth) continue;
      for (const next of [position - 1, position + 1, position - width, position + width]) {
        if (distance[next] || source[next * 4 + 3] <= 3) continue;
        distance[next] = depth + 1;
        queue[queueTail++] = next;
      }
    }

    for (const [x, y, offset, depth] of boundary) {
      const centerLuma = luminance(offset);
      const centerMax = Math.max(source[offset], source[offset + 1], source[offset + 2]);
      const centerMin = Math.min(source[offset], source[offset + 1], source[offset + 2]);
      const neutralLight = centerLuma > 150 && centerMax - centerMin < 64;
      let best = -1;
      let bestDistance = Infinity;
      const radius = neutralLight ? Math.min(12, 5 + maxDepth) : 4;

      for (let dy = -radius; dy <= radius; dy += 1) {
        const ny = y + dy;
        if (ny < 2 || ny >= height - 2) continue;
        for (let dx = -radius; dx <= radius; dx += 1) {
          const nx = x + dx;
          if (nx < 2 || nx >= width - 2) continue;
          const distance = dx * dx + dy * dy;
          if (!distance || distance >= bestDistance) continue;
          const candidate = index(nx, ny);
          if (source[candidate + 3] < 242) continue;
          if (alpha(nx - 1, ny) < 220 || alpha(nx + 1, ny) < 220 || alpha(nx, ny - 1) < 220 || alpha(nx, ny + 1) < 220) continue;
          if (neutralLight && luminance(candidate) > centerLuma - 34) continue;
          best = candidate;
          bestDistance = distance;
        }
      }

      if (best >= 0) {
        const lumaGap = centerLuma - luminance(best);
        const bandStrength = 1 - Math.min(0.72, (depth - 1) / Math.max(1, maxDepth));
        const strength = neutralLight ? Math.max(0.38, 0.94 * bandStrength) : Math.max(0.18, (248 - source[offset + 3]) / 255);
        pixels[offset] = Math.round(source[offset] * (1 - strength) + source[best] * strength);
        pixels[offset + 1] = Math.round(source[offset + 1] * (1 - strength) + source[best + 1] * strength);
        pixels[offset + 2] = Math.round(source[offset + 2] * (1 - strength) + source[best + 2] * strength);
        if ((atlas === "a" || atlas === "b") && neutralLight && lumaGap > 45) {
          pixels[offset + 3] = Math.min(pixels[offset + 3], depth <= 6 ? 0 : Math.round((depth - 6) * 74));
        }
      }

      if (pixels[offset + 3] > 0 && source[offset + 3] === 255 && (alpha(x - 1, y) <= 3 || alpha(x + 1, y) <= 3 || alpha(x, y - 1) <= 3 || alpha(x, y + 1) <= 3)) {
        pixels[offset + 3] = 176;
      } else if (source[offset + 3] < 248) {
        pixels[offset + 3] = Math.min(255, Math.round(Math.pow(source[offset + 3] / 255, 0.78) * 255));
      }
    }

    if (atlas === "a" || atlas === "b") {
      const cleanedAlpha = new Uint8ClampedArray(width * height);
      for (let position = 0; position < cleanedAlpha.length; position += 1) cleanedAlpha[position] = pixels[position * 4 + 3];
      for (let y = 1; y < height - 1; y += 1) {
        for (let x = 1; x < width - 1; x += 1) {
          const position = y * width + x;
          const offset = position * 4;
          if (cleanedAlpha[position] <= 3) continue;
          let coverage = 0;
          for (let dy = -1; dy <= 1; dy += 1) {
            for (let dx = -1; dx <= 1; dx += 1) {
              if (cleanedAlpha[position + dy * width + dx] > 24) coverage += 1;
            }
          }
          if (coverage <= 2) pixels[offset + 3] = 0;
          else if (coverage <= 4) pixels[offset + 3] = Math.min(pixels[offset + 3], 88);
          else if (coverage <= 6 && pixels[offset + 3] < 240) pixels[offset + 3] = Math.min(pixels[offset + 3], 168);
        }
      }
    }

    context.putImageData(frame, 0, 0);
    canvas.toBlob((blob) => {
      if (!blob) return;
      const url = URL.createObjectURL(blob);
      document.documentElement.style.setProperty("--house-atlas-" + atlas, `url("${url}")`);
    }, "image/png");
    return canvas;
  }

  class HouseCharacterRig {
    constructor(canvas, host) {
      this.canvas = canvas;
      this.host = host;
      this.gl = null;
      this.program = null;
      this.locations = {};
      this.textures = new Map();
      this.loading = new Map();
      this.sprite = "fan";
      this.atlas = "a";
      this.rig = RIGS.fan;
      this.mode = "mesh2d";
      this.enabled = true;
      this.intensity = "balanced";
      this.ready = false;
      this.failed = false;
      this.raf = 0;
      this.frameCount = 0;
      this.lastDrawAt = 0;
      this.action = null;
      this.lastOffsets = null;
      this.expression = "gentle";
      this.faceStyle = {...FACE_EXPRESSIONS.gentle};
      this.faceMotion = {blink: 0, gazeX: 0, gazeY: 0};
      this.nextBlinkAt = performance.now() + 1400 + Math.random() * 1800;
      this.blinkStartedAt = 0;
      this.blinkDuration = 300;
      this.doubleBlinkPending = false;
      this.nextGazeAt = performance.now() + 1800;
      this.gazeTarget = {x: 0, y: 0};
      this.pointer = {x: 0, y: 0, active: false};
      this.debugMode = new URLSearchParams(window.location.search).has("rig_debug");
      this.generation = 0;
      this.reducedQuery = matchMedia("(prefers-reduced-motion: reduce)");
      this.reduced = this.reducedQuery.matches;
      this._visibility = () => document.hidden ? this.stop() : this.start();
      this._pointerMove = (event) => {
        if (event.pointerType === "touch") return;
        const rect = this.host.getBoundingClientRect();
        this.pointer.x = Math.max(-1, Math.min(1, ((event.clientX - rect.left) / Math.max(1, rect.width) - 0.5) * 2));
        this.pointer.y = Math.max(-1, Math.min(1, ((event.clientY - rect.top) / Math.max(1, rect.height) - 0.5) * 2));
        this.pointer.active = true;
        this.nextGazeAt = performance.now() + 2600;
        this.start();
      };
      this._pointerLeave = () => {
        this.pointer.active = false;
        this.gazeTarget = {x: 0, y: 0};
      };
      this._motionChange = (event) => {
        this.reduced = event.matches;
        if (this.reduced) this.useFallback("reduced-motion");
        else this.configure(this.lastCharacter || {}, this.lastMotion || {});
      };
      document.addEventListener("visibilitychange", this._visibility);
      this.host.addEventListener("pointermove", this._pointerMove, {passive: true});
      this.host.addEventListener("pointerleave", this._pointerLeave, {passive: true});
      this.reducedQuery.addEventListener?.("change", this._motionChange);
      this.resizeObserver = new ResizeObserver(() => this.resize());
      this.resizeObserver.observe(this.host);
      this.initialize();
    }

    initialize() {
      try {
        const gl = this.canvas.getContext("webgl", {
          alpha: true,
          antialias: true,
          premultipliedAlpha: true,
          preserveDrawingBuffer: this.debugMode,
          powerPreference: "low-power",
        });
        if (!gl) throw new Error("WebGL unavailable");
        this.gl = gl;
        this.program = createProgram(gl);
        gl.useProgram(this.program);
        const mesh = createMesh(gl, 64, 64);
        const vertexBuffer = gl.createBuffer();
        gl.bindBuffer(gl.ARRAY_BUFFER, vertexBuffer);
        gl.bufferData(gl.ARRAY_BUFFER, mesh.vertices, gl.STATIC_DRAW);
        const stride = 4 * Float32Array.BYTES_PER_ELEMENT;
        const position = gl.getAttribLocation(this.program, "aPosition");
        const uv = gl.getAttribLocation(this.program, "aUv");
        gl.enableVertexAttribArray(position);
        gl.vertexAttribPointer(position, 2, gl.FLOAT, false, stride, 0);
        gl.enableVertexAttribArray(uv);
        gl.vertexAttribPointer(uv, 2, gl.FLOAT, false, stride, 2 * Float32Array.BYTES_PER_ELEMENT);
        const indexBuffer = gl.createBuffer();
        gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, indexBuffer);
        gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, mesh.indices, gl.STATIC_DRAW);
        this.indexCount = mesh.indices.length;
        for (const name of ["Head", "Hair", "Torso", "LeftSleeve", "RightSleeve", "Skirt", "Prop", "Offsets0", "Offsets1", "Offsets2", "Offsets3", "Cell", "Face", "FaceMotion", "FaceStyle"]) {
          this.locations[name] = gl.getUniformLocation(this.program, "u" + name);
        }
        this.locations.texture = gl.getUniformLocation(this.program, "uTexture");
        gl.uniform1i(this.locations.texture, 0);
        gl.clearColor(0, 0, 0, 0);
        this.resize();
      } catch (error) {
        this.failed = true;
        this.canvas.dataset.rigError = String(error && error.message || error || "webgl init failed");
        if (this.debugMode) console.warn("[house-rig]", this.canvas.dataset.rigError);
        this.useFallback("webgl-init");
      }
    }

    configure(character, motion) {
      this.lastCharacter = character;
      this.lastMotion = motion;
      this.generation += 1;
      const nextSprite = RIGS[character.sprite] ? character.sprite : "fan";
      const nextAtlas = /^[a-e]$/.test(character.atlas || "") ? character.atlas : "a";
      const nextRig = RIGS[nextSprite];
      this.mode = motion.character_render_mode === "classic" ? "classic" : "mesh2d";
      this.enabled = motion.enabled !== false;
      this.intensity = ["gentle", "balanced", "vivid"].includes(motion.intensity) ? motion.intensity : "balanced";
      this.setExpression(character.expression || nextRig.expression || "gentle");
      this.action = null;
      delete this.canvas.dataset.pixelSample;
      if (this.failed || this.reduced || !this.enabled || this.mode !== "mesh2d") {
        this.sprite = nextSprite;
        this.atlas = nextAtlas;
        this.rig = nextRig;
        this.useFallback(this.reduced ? "reduced-motion" : this.mode === "classic" ? "classic" : "disabled");
        return;
      }
      const ticket = this.generation;
      this.loadTexture(nextSprite).then((textureEntry) => {
        if (ticket !== this.generation) return;
        this.sprite = nextSprite;
        this.atlas = nextAtlas;
        this.rig = nextRig;
        this.textureEntry = textureEntry;
        this.ready = true;
        this.host.classList.add("rig-ready");
        this.host.classList.remove("rig-fallback");
        this.canvas.dataset.renderer = "webgl-mesh2d";
        this.canvas.dataset.sprite = this.sprite;
        this.canvas.dataset.rigProfiles = String(Object.keys(RIGS).length);
        this.canvas.dataset.channels = "head,hair,torso,left_sleeve,right_sleeve,skirt,prop,face,blink,gaze,mouth";
        this.resize();
        this.start();
      }).catch(() => this.useFallback("texture-load"));
    }

    loadTexture(sprite) {
      if (this.textures.has(sprite)) return Promise.resolve(this.textures.get(sprite));
      if (this.loading.has(sprite)) return this.loading.get(sprite);
      const promise = new Promise((resolve, reject) => {
        const image = new Image();
        image.decoding = "async";
        image.onload = () => {
          try {
            const gl = this.gl;
            const texture = gl.createTexture();
            gl.activeTexture(gl.TEXTURE0);
            gl.bindTexture(gl.TEXTURE_2D, texture);
            gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, true);
            gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
            gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
            gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
            gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
            gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, image);
            const entry = {texture, width: image.naturalWidth, height: image.naturalHeight};
            this.textures.set(sprite, entry);
            resolve(entry);
          } catch (error) {
            reject(error);
          }
        };
        image.onerror = reject;
        image.src = "/assets/house-character-" + sprite + ".png";
      }).finally(() => this.loading.delete(sprite));
      this.loading.set(sprite, promise);
      return promise;
    }

    useFallback(reason) {
      this.ready = false;
      this.stop();
      this.host.classList.remove("rig-ready");
      this.host.classList.add("rig-fallback");
      this.canvas.dataset.renderer = "classic";
      this.canvas.dataset.fallbackReason = reason || "unknown";
    }

    resize() {
      if (!this.gl) return;
      const rect = this.host.getBoundingClientRect();
      const dpr = Math.min(1.6, window.devicePixelRatio || 1);
      const width = Math.max(2, Math.round(rect.width * dpr));
      const height = Math.max(2, Math.round(rect.height * dpr));
      if (this.canvas.width !== width || this.canvas.height !== height) {
        this.canvas.width = width;
        this.canvas.height = height;
        this.gl.viewport(0, 0, width, height);
      }
    }

    start() {
      if (!this.ready || document.hidden || this.raf) return;
      this.raf = requestAnimationFrame((time) => this.frame(time));
    }

    stop() {
      if (this.raf) cancelAnimationFrame(this.raf);
      this.raf = 0;
    }

    play(primitive, amplitude, duration) {
      if (!this.ready) return;
      this.action = {
        name: primitive || "breathe",
        amplitude: Math.max(0.15, Math.min(1.4, Number(amplitude) || 0.45)),
        started: performance.now(),
        duration: Math.max(0.6, Number(duration) || 3.5) * 1000,
      };
      this.start();
    }

    react(name) {
      const map = {soft: "settle", still: "stillness"};
      this.play(map[name] || name || "nod", 1.18, name === "still" ? 4.0 : 1.25);
    }

    normalizeExpression(value) {
      const raw = String(value || "gentle");
      const key = FACE_ALIASES[raw] || raw;
      return FACE_EXPRESSIONS[key] ? key : "gentle";
    }

    setExpression(value, immediate) {
      this.expression = this.normalizeExpression(value);
      if (immediate) this.faceStyle = {...FACE_EXPRESSIONS[this.expression]};
      this.start();
      return this.expression;
    }

    cycleExpression() {
      const order = ["gentle", "bright", "curious", "playful", "attentive", "wistful", "sleepy", "surprised"];
      const index = Math.max(0, order.indexOf(this.expression));
      return this.setExpression(order[(index + 1) % order.length]);
    }

    forceBlink(duration) {
      this.blinkStartedAt = performance.now();
      this.blinkDuration = Math.max(220, Math.min(900, Number(duration) || 520));
      this.doubleBlinkPending = false;
      this.start();
    }

    updateFace(now) {
      if (!this.blinkStartedAt && now >= this.nextBlinkAt) {
        this.blinkStartedAt = now;
        this.blinkDuration = 260 + Math.random() * 100;
        this.doubleBlinkPending = Math.random() < 0.16;
      }
      let blink = 0;
      if (this.blinkStartedAt) {
        const phase = (now - this.blinkStartedAt) / this.blinkDuration;
        if (phase >= 1) {
          this.blinkStartedAt = 0;
          if (this.doubleBlinkPending) {
            this.doubleBlinkPending = false;
            this.nextBlinkAt = now + 120;
          } else {
            const fatigueDelay = this.expression === "sleepy" ? 2200 : 3400;
            this.nextBlinkAt = now + fatigueDelay + Math.random() * 3600;
          }
        } else {
          blink = Math.sin(Math.PI * phase);
        }
      }
      if (this.pointer.active) {
        this.gazeTarget.x = this.pointer.x * 0.68;
        this.gazeTarget.y = this.pointer.y * 0.24;
      } else if (now >= this.nextGazeAt) {
        const quiet = ["sleepy", "melancholy", "wistful", "pensive"].includes(this.expression);
        this.gazeTarget.x = (Math.random() * 2 - 1) * (quiet ? 0.32 : 0.55);
        this.gazeTarget.y = (Math.random() * 2 - 1) * 0.22;
        this.nextGazeAt = now + 3200 + Math.random() * 5200;
      }
      this.faceMotion.gazeX += (this.gazeTarget.x - this.faceMotion.gazeX) * 0.035;
      this.faceMotion.gazeY += (this.gazeTarget.y - this.faceMotion.gazeY) * 0.035;
      const target = FACE_EXPRESSIONS[this.expression] || FACE_EXPRESSIONS.gentle;
      for (const key of ["lid", "smile", "mouth", "gazeY"]) {
        this.faceStyle[key] += (target[key] - this.faceStyle[key]) * 0.08;
      }
      let actionPulse = 0;
      let actionName = "";
      if (this.action) {
        const phase = (now - this.action.started) / this.action.duration;
        if (phase >= 0 && phase < 1) {
          actionPulse = easePulse(phase) * this.action.amplitude;
          actionName = this.action.name;
        }
      }
      if (actionName === "startle") blink *= 0.05;
      if (["greet", "sip", "tend", "page"].includes(actionName)) this.faceStyle.smile += actionPulse * 0.14;
      if (actionName === "sip") this.faceStyle.mouth += actionPulse * 0.22;
      if (actionName === "yawn") this.faceStyle.mouth += actionPulse * 0.58;
      this.faceMotion.blink = Math.max(0, Math.min(1, blink));
      return {
        motion: [
          this.faceMotion.blink,
          this.faceMotion.gazeX,
          this.faceMotion.gazeY + this.faceStyle.gazeY,
          this.faceStyle.smile,
        ],
        style: [
          Math.max(-0.22, Math.min(0.62, this.faceStyle.lid)),
          Math.max(-0.85, Math.min(0.92, this.faceStyle.smile)),
          Math.max(0, Math.min(0.85, this.faceStyle.mouth)),
          0,
        ],
      };
    }

    actionOffsets(now) {
      const result = {
        headX: 0, headY: 0, hairX: 0, hairY: 0, torsoX: 0, torsoY: 0,
        leftX: 0, leftY: 0, rightX: 0, rightY: 0, skirtX: 0, skirtY: 0,
        propX: 0, propY: 0, headRot: 0, expand: 0, quiet: 1,
      };
      const action = this.action;
      if (!action) return result;
      const phase = (now - action.started) / action.duration;
      if (phase >= 1) {
        this.action = null;
        return result;
      }
      const p = easePulse(phase) * action.amplitude;
      const wave = Math.sin(phase * Math.PI * 2) * action.amplitude;
      switch (action.name) {
        case "sway": result.headX = p * 0.010; result.hairX = p * 0.016; result.torsoX = p * 0.006; result.skirtX = p * 0.009; break;
        case "turn": result.headX = p * 0.012; result.hairX = p * 0.018; result.headRot = p * 0.025; break;
        case "nod": result.headY = p * 0.014; result.hairY = p * 0.006; result.headRot = wave * 0.010; break;
        case "settle": result.headY = p * 0.007; result.torsoY = p * 0.006; result.leftY = p * 0.007; result.rightY = p * 0.007; result.skirtY = p * 0.004; break;
        case "startle": result.headY = -p * 0.020; result.hairY = -p * 0.013; result.torsoY = -p * 0.010; result.leftY = -p * 0.008; result.rightY = -p * 0.008; result.expand = p * 0.010; break;
        case "recoil": result.headX = -p * 0.012; result.hairX = -p * 0.018; result.torsoX = -p * 0.008; result.headRot = -p * 0.018; break;
        case "lean": result.headY = -p * 0.008; result.torsoY = -p * 0.006; result.expand = p * 0.014; result.headRot = p * 0.010; break;
        case "write": result.rightX = wave * 0.010; result.rightY = p * 0.008; result.propX = wave * 0.008; result.propY = p * 0.005; break;
        case "dip": result.headY = p * 0.016; result.hairY = p * 0.009; result.torsoY = p * 0.004; break;
        case "lift": result.leftY = -p * 0.014; result.rightY = -p * 0.012; result.propY = -p * 0.016; result.headY = -p * 0.004; break;
        case "greet": result.rightX = p * 0.013; result.rightY = -p * 0.017; result.propX = p * 0.008; result.headRot = -p * 0.010; break;
        case "sip": result.rightY = -p * 0.030; result.rightX = -p * 0.010; result.propY = -p * 0.032; result.propX = -p * 0.010; result.headY = p * 0.006; result.headRot = -p * 0.010; break;
        case "pour": result.rightX = p * 0.020; result.rightY = p * 0.010; result.propX = p * 0.024; result.propY = p * 0.008; result.headRot = p * 0.012; break;
        case "tend": result.leftY = p * 0.022; result.rightY = p * 0.018; result.leftX = -p * 0.012; result.propY = p * 0.018; result.headY = p * 0.012; break;
        case "page": result.leftX = p * 0.013; result.rightX = -p * 0.016; result.rightY = -p * 0.009; result.propX = wave * 0.012; result.headY = p * 0.006; break;
        case "warm": result.leftX = p * 0.008; result.rightX = -p * 0.008; result.leftY = -p * 0.010; result.rightY = -p * 0.010; result.propY = -p * 0.008; break;
        case "listen": result.headRot = p * 0.020; result.headX = p * 0.008; result.hairX = p * 0.012; break;
        case "yawn": result.headY = p * 0.010; result.leftY = -p * 0.022; result.leftX = p * 0.008; result.torsoY = p * 0.006; break;
        case "stillness": result.quiet = Math.max(0.06, 1 - p * 0.94); break;
        case "breathe": result.expand = p * 0.010; result.torsoY = -p * 0.004; break;
        default: result.headY = p * 0.006; result.hairY = p * 0.004;
      }
      return result;
    }

    frame(now) {
      this.raf = 0;
      if (!this.ready || !this.textureEntry || document.hidden) return;
      if (now - this.lastDrawAt < 31) {
        this.raf = requestAnimationFrame((time) => this.frame(time));
        return;
      }
      this.lastDrawAt = now;
      this.resize();
      const gl = this.gl;
      const rig = this.rig;
      const strength = intensityValue(this.intensity);
      const t = now / 1000 + rig.phase;
      const action = this.actionOffsets(now);
      const quiet = action.quiet;
      const breath = Math.sin(t * 1.16) * strength * quiet;
      const drift = Math.sin(t * 0.72 + 0.8) * strength * quiet;
      const loose = Math.sin(t * 0.54 + 1.7) * strength * quiet;
      const sleeve = Math.sin(t * 0.88 + 2.1) * strength * quiet;
      const face = this.updateFace(now);
      const pointerX = this.pointer.active ? this.pointer.x * strength : 0;
      const pointerY = this.pointer.active ? this.pointer.y * strength : 0;
      this.lastFace = {
        expression: this.expression,
        blink: face.motion[0],
        gaze: [face.motion[1], face.motion[2]],
        smile: face.style[1],
        mouth: face.style[2],
      };

      const offsets0 = [
        drift * 0.0022 + pointerX * 0.0018 + action.headX,
        breath * 0.0011 + pointerY * 0.0008 + action.headY,
        loose * 0.0052 + pointerX * 0.0024 + action.hairX,
        breath * 0.0017 + pointerY * 0.0011 + action.hairY,
      ];
      const offsets1 = [
        drift * 0.0012 + pointerX * 0.0005 + action.torsoX,
        breath * -0.0019 + pointerY * 0.0003 + action.torsoY,
        sleeve * -0.0021 + action.leftX,
        breath * 0.0024 + action.leftY,
      ];
      const offsets2 = [
        sleeve * 0.0021 + action.rightX,
        breath * 0.0021 + action.rightY,
        loose * 0.0032 + action.skirtX,
        breath * 0.0012 + action.skirtY,
      ];
      const offsets3 = [
        sleeve * 0.0024 + action.propX,
        breath * 0.0018 + action.propY,
        drift * 0.0075 + pointerX * 0.0028 + action.headRot,
        breath * 0.0042 + action.expand,
      ];
      this.lastOffsets = {
        head: offsets0.slice(0, 2),
        hair: offsets0.slice(2, 4),
        torso: offsets1.slice(0, 2),
        left_sleeve: offsets1.slice(2, 4),
        right_sleeve: offsets2.slice(0, 2),
        skirt: offsets2.slice(2, 4),
        prop: offsets3.slice(0, 2),
        head_rotation: offsets3[2],
        torso_expansion: offsets3[3],
      };
      const insetX = 0.5 / this.textureEntry.width;
      const insetY = 0.5 / this.textureEntry.height;

      gl.clear(gl.COLOR_BUFFER_BIT);
      gl.useProgram(this.program);
      gl.activeTexture(gl.TEXTURE0);
      gl.bindTexture(gl.TEXTURE_2D, this.textureEntry.texture);
      for (const key of ["Head", "Hair", "Torso", "LeftSleeve", "RightSleeve", "Skirt", "Prop"]) {
        const sourceKey = key.charAt(0).toLowerCase() + key.slice(1);
        gl.uniform4fv(this.locations[key], rig[sourceKey]);
      }
      gl.uniform4fv(this.locations.Offsets0, offsets0);
      gl.uniform4fv(this.locations.Offsets1, offsets1);
      gl.uniform4fv(this.locations.Offsets2, offsets2);
      gl.uniform4fv(this.locations.Offsets3, offsets3);
      gl.uniform4fv(this.locations.Face, rig.face);
      gl.uniform4fv(this.locations.FaceMotion, face.motion);
      gl.uniform4fv(this.locations.FaceStyle, face.style);
      gl.uniform4f(
        this.locations.Cell,
        insetX,
        insetY,
        1.0 - insetX * 2,
        1.0 - insetY * 2,
      );
      gl.drawElements(gl.TRIANGLES, this.indexCount, gl.UNSIGNED_SHORT, 0);
      this.frameCount += 1;
      if (this.debugMode && this.frameCount % 2 === 0) {
        this.canvas.dataset.frames = String(this.frameCount);
        this.canvas.dataset.offsets = JSON.stringify(this.lastOffsets);
        this.canvas.dataset.face = JSON.stringify({
          expression: this.expression,
          blink: Number(face.motion[0].toFixed(3)),
          gaze: [Number(face.motion[1].toFixed(3)), Number(face.motion[2].toFixed(3))],
          smile: Number(face.style[1].toFixed(3)),
          mouth: Number(face.style[2].toFixed(3)),
        });
      }
      if (this.debugMode && this.frameCount % 10 === 0) {
        const pixels = new Uint8Array(this.canvas.width * this.canvas.height * 4);
        gl.readPixels(0, 0, this.canvas.width, this.canvas.height, gl.RGBA, gl.UNSIGNED_BYTE, pixels);
        let nonzero = 0;
        let minX = this.canvas.width;
        let minY = this.canvas.height;
        let maxX = -1;
        let maxY = -1;
        for (let y = 0; y < this.canvas.height; y += 4) {
          for (let x = 0; x < this.canvas.width; x += 4) {
            if (pixels[(y * this.canvas.width + x) * 4 + 3] <= 8) continue;
            nonzero += 1;
            minX = Math.min(minX, x);
            minY = Math.min(minY, y);
            maxX = Math.max(maxX, x);
            maxY = Math.max(maxY, y);
          }
        }
        this.canvas.dataset.pixelSample = JSON.stringify({
          nonzero,
          bbox: [minX, minY, maxX, maxY],
          step: 4,
        });
      }
      this.raf = requestAnimationFrame((time) => this.frame(time));
    }

    debug() {
      return {
        renderer: this.ready ? "webgl-mesh2d" : "classic",
        ready: this.ready,
        failed: this.failed,
        mode: this.mode,
        sprite: this.sprite,
        atlas: this.atlas,
        atlasSize: this.textureEntry ? [this.textureEntry.width, this.textureEntry.height] : [0, 0],
        frames: this.frameCount,
        action: this.action ? this.action.name : "",
        canvas: [this.canvas.width, this.canvas.height],
        channels: ["head", "hair", "torso", "left_sleeve", "right_sleeve", "skirt", "prop", "face", "blink", "gaze", "mouth"],
        mesh: [64, 64],
        expression: this.expression,
        face: this.lastFace || {},
        pointer: {...this.pointer},
        rigProfiles: Object.keys(RIGS).length,
        offsets: this.lastOffsets,
      };
    }

    destroy() {
      this.stop();
      document.removeEventListener("visibilitychange", this._visibility);
      this.host.removeEventListener("pointermove", this._pointerMove);
      this.host.removeEventListener("pointerleave", this._pointerLeave);
      this.reducedQuery.removeEventListener?.("change", this._motionChange);
      this.resizeObserver.disconnect();
    }
  }

  window.HouseCharacterRig = HouseCharacterRig;
  window.HOUSE_CHARACTER_RIG_PROFILES = Object.freeze(Object.keys(RIGS));
})();
