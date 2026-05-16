/* ==============================================
   Backend URL
============================================== */
const BACKEND_URL = (window.BACKEND_URL || 'https://cinema-764066091864.europe-west1.run.app').replace(/\/$/, '');

/* ==============================================
   State
============================================== */
const state = {
  tab: 'archive',
  filters: {},
  sortBy: 'year',
  sortDir: 'desc',
  page: 1,
  perPage: 24,
  totalPages: 1,
  listType: 'seen',
  fullRescrape: false,
  scrapeId: null,
  scrapeInterval: null,
  currentMovie: null,
  currentMovieIndex: -1,
  movieList: [],
  userMovies: [],
  abortController: null,
};

/* ==============================================
   API helper
============================================== */
async function api(path, options = {}) {
  if (state.abortController && options._abort) {
    state.abortController.abort();
  }
  const controller = new AbortController();
  if (options._abort) state.abortController = controller;
  const { _abort, ...fetchOptions } = options;

  try {
    const res = await fetch(BACKEND_URL + path, {
      ...fetchOptions,
      signal: controller.signal,
      headers: { 'Content-Type': 'application/json', ...(fetchOptions.headers || {}) },
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ error: res.statusText }));
      throw new Error(err.error || res.statusText);
    }
    return await res.json();
  } catch (e) {
    if (e.name === 'AbortError') return null;
    throw e;
  }
}

/* ==============================================
   Toast
============================================== */
function toast(msg, type = 'info', duration = 3500) {
  const wrap = document.getElementById('toastContainer');
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.textContent = msg;
  wrap.appendChild(el);
  setTimeout(() => el.remove(), duration);
}

/* ==============================================
   Helpers
============================================== */
function escHtml(str) {
  return String(str || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function renderStars(stars) {
  if (!stars && stars !== 0) return '';
  const full  = Math.floor(stars);
  const half  = stars % 1 >= 0.5 ? 1 : 0;
  const empty = 5 - full - half;
  return '★'.repeat(full) + (half ? '½' : '') + '☆'.repeat(empty);
}

function formatDuration(minutes) {
  if (!minutes) return null;
  const mins = parseInt(minutes);
  if (isNaN(mins)) return String(minutes);
  const h = Math.floor(mins / 60);
  const m = mins % 60;
  if (h === 0) return `${m}λ`;
  if (m === 0) return `${h}ω`;
  return `${h}ω ${m}λ`;
}

function posterImg(url, title) {
  if (url) {
    return `<img class="card-poster" src="${escHtml(url)}" alt="${escHtml(title)}" loading="lazy" onerror="this.replaceWith(posterPlaceholder('${escHtml(title)}'))" />`;
  }
  return `<div class="card-poster-placeholder"><span class="film-icon">🎬</span><span>${escHtml(title)}</span></div>`;
}

function posterPlaceholder(title) {
  const el = document.createElement('div');
  el.className = 'card-poster-placeholder';
  el.innerHTML = `<span class="film-icon">🎬</span><span>${escHtml(title)}</span>`;
  return el;
}

function renderSkeletons(count = 24) {
  return Array.from({ length: count }, () => `
    <div class="skeleton-card">
      <div class="skeleton skeleton-poster"></div>
      <div class="skeleton skeleton-text"></div>
      <div class="skeleton skeleton-text-sm"></div>
    </div>`).join('');
}

function getUserMovieBadges(title) {
  const badges = [];
  const t = (title || '').toLowerCase();
  const um = state.userMovies.filter(m => (m.title || '').toLowerCase() === t);
  const types = um.map(m => m.list_type);
  if (types.includes('seen') || types.includes('series_seen'))
    badges.push('<div class="badge badge-seen">👁 Είδα</div>');
  if (types.includes('favourite') || types.includes('series_favourite'))
    badges.push('<div class="badge badge-fav">⭐ Αγαπημένο</div>');
  return badges.join('');
}

/* ==============================================
   Movie card
============================================== */
function renderMovieCard(movie, isUserMovie = false, index = -1) {
  const badges = isUserMovie ? '' : getUserMovieBadges(movie.title);
  const listTypeBadge = isUserMovie
    ? `<div class="badge badge-seen" style="font-size:0.7rem">${escHtml(movie.list_type || '')}</div>` : '';

  const originalTitle = movie.title_original || '';
  const dur = formatDuration(movie.duration);
  const genre = Array.isArray(movie.genre) ? movie.genre.join(', ') : (movie.genre || '');
  const metaParts = [movie.year, movie.country, dur].filter(Boolean);

  const card = document.createElement('div');
  card.className = 'movie-card';
  card.dataset.id = movie.id || '';
  card.dataset.index = index;

  card.innerHTML = `
    ${badges ? `<div class="card-badges">${badges}</div>` : ''}
    ${isUserMovie ? `<div class="card-badges">${listTypeBadge}</div>` : ''}
    ${posterImg(movie.poster_url || movie.poster, movie.title)}
    <div class="card-body">
      <div class="card-title">${escHtml(movie.title)}</div>
      ${originalTitle ? `<div class="card-original">${escHtml(originalTitle)}</div>` : ''}
      ${movie.stars ? `
        <div class="card-stars">
          <span class="stars-display">${renderStars(movie.stars)}</span>
          <span class="stars-number">${movie.stars}</span>
        </div>` : ''}
      ${metaParts.length ? `<div class="card-meta">${metaParts.map(escHtml).join(' · ')}</div>` : ''}
      ${genre ? `<div class="card-genre">${escHtml(genre)}</div>` : ''}
    </div>
    ${isUserMovie ? `<button class="user-card-del" data-docid="${escHtml(movie.id)}" title="Διαγραφή">✕</button>` : ''}
  `;

  card.addEventListener('click', (e) => {
    if (e.target.closest('.user-card-del')) return;
    if (!isUserMovie) state.currentMovieIndex = index;
    openModal(movie, isUserMovie);
  });

  if (isUserMovie) {
    card.querySelector('.user-card-del').addEventListener('click', (e) => {
      e.stopPropagation();
      deleteUserMovie(movie.id, card);
    });
  }

  return card;
}

/* ==============================================
   Load movies (Archive tab)
============================================== */
async function loadMovies() {
  const grid = document.getElementById('movieGrid');
  grid.innerHTML = renderSkeletons(state.perPage);

  const params = new URLSearchParams({
    sort_by: state.sortBy,
    sort_dir: state.sortDir,
    page: state.page,
    per_page: state.perPage,
  });

  const f = state.filters;
  if (f.q)        params.set('q', f.q);
  if (f.yearFrom) params.set('year_from', f.yearFrom);
  if (f.yearTo)   params.set('year_to', f.yearTo);
  if (f.imdbMin) params.set('imdb_min', f.imdbMin);
  if (f.imdbMax) params.set('imdb_max', f.imdbMax);
  if (f.country)  params.set('country', f.country);
  if (f.genre)    params.set('genre', f.genre);
  if (f.durMin)   params.set('duration_min', f.durMin);
  if (f.durMax)   params.set('duration_max', f.durMax);

  try {
    const data = await api(`/api/movies?${params}`, { _abort: true });
    if (!data) return;

    state.totalPages = data.pages || 1;
    state.movieList = data.movies || [];

    const countEl = document.getElementById('resultsCount');
    if (countEl) countEl.textContent = `${data.total.toLocaleString('el-GR')} ταινίες`;

    updatePageSelect();

    grid.innerHTML = '';
    if (!state.movieList.length) {
      grid.innerHTML = `
        <div class="empty-state">
          <div class="empty-icon">🎬</div>
          <h3>Δεν βρέθηκαν ταινίες</h3>
          <p>Δοκίμασε διαφορετικά φίλτρα ή ξεκίνα το scraping.</p>
        </div>`;
    } else {
      state.movieList.forEach((m, i) => grid.appendChild(renderMovieCard(m, false, i)));
    }

    renderPagination();
  } catch (e) {
    grid.innerHTML = `
      <div class="empty-state">
        <div class="empty-icon">⚠</div>
        <h3>Σφάλμα σύνδεσης</h3>
        <p>${escHtml(e.message)}</p>
      </div>`;
  }
}

/* ==============================================
   Total count in header
============================================== */
async function loadTotalCount() {
  try {
    const data = await api('/api/movies?per_page=1');
    const el = document.getElementById('movieTotalCount');
    if (el && data && data.total) {
      el.textContent = `${data.total.toLocaleString('el-GR')} ταινίες στη βάση`;
    }
  } catch (e) {}
}

/* ==============================================
   Page select (toolbar dropdown)
============================================== */
function updatePageSelect() {
  const sel = document.getElementById('pageSelect');
  if (!sel) return;
  const current = state.page;
  sel.innerHTML = '';
  for (let i = 1; i <= state.totalPages; i++) {
    const opt = document.createElement('option');
    opt.value = i;
    opt.textContent = i;
    if (i === current) opt.selected = true;
    sel.appendChild(opt);
  }
}

/* ==============================================
   Pagination buttons
============================================== */
function renderPagination() {
  const wrap = document.getElementById('pagination');
  wrap.innerHTML = '';
  const { page, totalPages } = state;
  if (totalPages <= 1) return;

  const mkBtn = (label, p, active = false, disabled = false) => {
    const btn = document.createElement('button');
    btn.className = 'page-btn' + (active ? ' active' : '');
    btn.textContent = label;
    btn.disabled = disabled;
    if (!disabled && !active) btn.addEventListener('click', () => goToPage(p));
    return btn;
  };

  wrap.appendChild(mkBtn('‹', page - 1, false, page === 1));

  const pages = [];
  if (totalPages <= 7) {
    for (let i = 1; i <= totalPages; i++) pages.push(i);
  } else {
    pages.push(1);
    if (page > 3) pages.push('…');
    for (let i = Math.max(2, page - 1); i <= Math.min(totalPages - 1, page + 1); i++) pages.push(i);
    if (page < totalPages - 2) pages.push('…');
    pages.push(totalPages);
  }

  pages.forEach(p => {
    if (p === '…') {
      const el = document.createElement('span');
      el.className = 'page-ellipsis';
      el.textContent = '…';
      wrap.appendChild(el);
    } else {
      wrap.appendChild(mkBtn(p, p, p === page));
    }
  });

  wrap.appendChild(mkBtn('›', page + 1, false, page === totalPages));
}

function goToPage(p) {
  state.page = p;
  loadMovies();
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

/* ==============================================
   Random movie
============================================== */
async function loadRandom() {
  try {
    const movie = await api('/api/movies/random');
    if (movie) {
      state.currentMovieIndex = -1;
      openModal(movie, false);
    }
  } catch (e) {
    toast('Σφάλμα: ' + e.message, 'error');
  }
}

/* ==============================================
   Filter metadata
============================================== */
async function loadFiltersMeta() {
  try {
    const data = await api('/api/filters/meta');
    const cs = document.getElementById('countrySelect');
    const gs = document.getElementById('genreSelect');
    const lg = document.getElementById('listGenre');

    data.countries?.forEach(c => {
      cs.add(new Option(c, c));
    });
    data.genres?.forEach(g => {
      gs.add(new Option(g, g));
      lg?.add(new Option(g, g));
    });
  } catch (e) {}
}

/* ==============================================
   Modal
============================================== */
function openModal(movie, isUserEntry = false) {
  state.currentMovie = movie;

  document.getElementById('modalTitle').textContent = movie.title || '—';
  document.getElementById('modalOriginal').textContent = movie.title_original || '';

  // Stars (Athinorama) — κρυμμένα
  document.getElementById('modalStars').innerHTML = '';

  // Poster
  const pWrap = document.getElementById('modalPoster');
  const pUrl = movie.poster_url || movie.poster || '';
  if (pUrl) {
    pWrap.innerHTML = `<img src="${escHtml(pUrl)}" alt="${escHtml(movie.title)}" onerror="this.parentElement.innerHTML='<div class=modal-poster-placeholder>🎬</div>'" />`;
  } else {
    pWrap.innerHTML = '<div class="modal-poster-placeholder">🎬</div>';
  }

  // Meta (includes cast)
  const genre    = Array.isArray(movie.genre)    ? movie.genre.join(', ')    : (movie.genre || '');
  const director = Array.isArray(movie.director) ? movie.director.join(', ') : (movie.director || '');
  const cast     = Array.isArray(movie.cast)     ? movie.cast.join(', ')     : (movie.cast || '');
  const dur      = movie.duration ? `${formatDuration(movie.duration)} (${movie.duration} λεπτά)` : null;

  const meta = [
    ['Έτος',       movie.year],
    ['Χώρα',       movie.country],
    ['Είδος',      genre],
    ['Σκηνοθεσία', director],
    ['Διάρκεια',   dur],
    ['Ηθοποιοί',   cast],
  ].filter(([, v]) => v);

  document.getElementById('modalMeta').innerHTML = meta.map(([l, v]) => `
    <div class="modal-meta-row">
      <span class="modal-meta-label">${escHtml(l)}</span>
      <span class="modal-meta-value">${escHtml(String(v))}</span>
    </div>`).join('');

  // Scores — μόνο IMDb
  const scores = [];
  if (movie.imdb_score) scores.push(['IMDb', movie.imdb_score]);
  document.getElementById('modalScores').innerHTML = scores.map(([l, v]) =>
    `<div class="score-badge"><div class="score-label">${escHtml(l)}</div><div class="score-val">${escHtml(String(v))}</div></div>`
  ).join('');

  // Description
  const desc = movie.description || '';
  const descSec = document.getElementById('modalDescSection');
  document.getElementById('modalDesc').textContent = desc;
  descSec.style.display = desc ? '' : 'none';

  // Similar
  const similar = movie.similar || '';
  const simSec = document.getElementById('modalSimilarSection');
  if (simSec) {
    document.getElementById('modalSimilar').textContent = similar;
    simSec.style.display = similar ? '' : 'none';
  }

  // Athinorama link
  const athLink = document.getElementById('modalAthinoramaLink');
  const url = movie.athinorama_url || '';
  athLink.href = url;
  athLink.style.display = url ? '' : 'none';

  // IMDb link
  const imdbLink = document.getElementById('modalImdbLink');
  imdbLink.classList.add('hidden');
  if (movie.imdb_url) {
    imdbLink.href = movie.imdb_url;
    imdbLink.classList.remove('hidden');
  } else if (movie.id) {
    api(`/api/movies/${encodeURIComponent(movie.id)}/imdb`)
      .then(res => {
        if (res && res.imdb_url) {
          imdbLink.href = res.imdb_url;
          imdbLink.classList.remove('hidden');
          movie.imdb_url = res.imdb_url;
        }
      })
      .catch(() => {});
  }

  // Trailer footer link
  const trLink = document.getElementById('modalTrailerLink');
  const trailer = movie.trailer_link || '';
  trLink.href = trailer;
  trLink.classList.toggle('hidden', !trailer);

  // Trailer embed
  const trailerSec  = document.getElementById('modalTrailerSection');
  const trailerWrap = document.getElementById('modalTrailerWrap');
  const trailerLoad = document.getElementById('modalTrailerLoading');
  trailerSec.style.display = '';
  trailerLoad.style.display = 'flex';
  trailerWrap.querySelectorAll('iframe').forEach(f => f.remove());

  if (movie.yt_trailer_id) {
    _embedTrailer(movie.yt_trailer_id);
  } else {
    api(`/api/movies/${encodeURIComponent(movie.id)}/trailer`)
      .then(res => {
        if (res && res.video_id) _embedTrailer(res.video_id);
        else trailerSec.style.display = 'none';
      })
      .catch(() => { trailerSec.style.display = 'none'; });
  }

  // Prev/Next nav buttons
  _updateModalNav(isUserEntry);

  document.getElementById('movieModal').classList.remove('hidden');
  document.body.style.overflow = 'hidden';
}

function _updateModalNav(isUserEntry) {
  const prevBtn = document.getElementById('modalPrevBtn');
  const nextBtn = document.getElementById('modalNextBtn');
  const idx = state.currentMovieIndex;
  const list = state.movieList;

  if (isUserEntry || idx < 0 || !list.length) {
    prevBtn.disabled = true;
    nextBtn.disabled = true;
  } else {
    prevBtn.disabled = idx <= 0;
    nextBtn.disabled = idx >= list.length - 1;
  }
}

function _embedTrailer(videoId) {
  const trailerSec  = document.getElementById('modalTrailerSection');
  const trailerWrap = document.getElementById('modalTrailerWrap');
  const trailerLoad = document.getElementById('modalTrailerLoading');
  trailerLoad.style.display = 'none';
  trailerSec.style.display = '';
  const iframe = document.createElement('iframe');
  iframe.src = `https://www.youtube-nocookie.com/embed/${videoId}?rel=0&modestbranding=1`;
  iframe.allow = 'accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture';
  iframe.allowFullscreen = true;
  trailerWrap.appendChild(iframe);
}

function closeModal() {
  document.getElementById('modalTrailerWrap').querySelectorAll('iframe').forEach(f => f.remove());
  document.getElementById('movieModal').classList.add('hidden');
  document.body.style.overflow = '';
}

/* ==============================================
   Modal navigation
============================================== */
document.getElementById('modalPrevBtn').addEventListener('click', () => {
  const idx = state.currentMovieIndex - 1;
  if (idx >= 0 && idx < state.movieList.length) {
    state.currentMovieIndex = idx;
    openModal(state.movieList[idx], false);
  }
});

document.getElementById('modalNextBtn').addEventListener('click', () => {
  const idx = state.currentMovieIndex + 1;
  if (idx >= 0 && idx < state.movieList.length) {
    state.currentMovieIndex = idx;
    openModal(state.movieList[idx], false);
  }
});

document.getElementById('modalRandomBtn').addEventListener('click', loadRandom);

/* ==============================================
   Add to list
============================================== */
document.getElementById('addToListBtn').addEventListener('click', () => {
  document.getElementById('addToListMenu').classList.toggle('open');
});

document.getElementById('addToListMenu').querySelectorAll('.dropdown-item').forEach(btn => {
  btn.addEventListener('click', async () => {
    document.getElementById('addToListMenu').classList.remove('open');
    const listType = btn.dataset.listtype;
    const m = state.currentMovie;
    if (!m) return;

    const entry = {
      title:         m.title || '',
      title_greek:   m.title || '',
      list_type:     listType,
      year:          m.year || null,
      genre:         Array.isArray(m.genre)    ? m.genre.join(', ')    : (m.genre || ''),
      director:      Array.isArray(m.director) ? m.director.join(', ') : (m.director || ''),
      cast:          Array.isArray(m.cast)     ? m.cast.join(', ')     : (m.cast || ''),
      poster:        m.poster_url || m.poster || '',
      duration:      m.duration ? String(m.duration) : '',
      athinorama_id: m.id || null,
    };

    try {
      await api('/api/user/movies', { method: 'POST', body: JSON.stringify(entry) });
      toast(`"${m.title}" προστέθηκε στη λίστα!`, 'success');
      loadUserMovies();
    } catch (e) {
      toast('Σφάλμα: ' + e.message, 'error');
    }
  });
});

/* ==============================================
   User movies (My List)
============================================== */
async function loadUserMovies() {
  const grid = document.getElementById('userGrid');
  grid.innerHTML = renderSkeletons(8);

  try {
    const data = await api(`/api/user/movies?list_type=${state.listType}`);
    state.userMovies = data.movies || [];
    renderUserGrid();
  } catch (e) {
    grid.innerHTML = `<div class="empty-state"><div class="empty-icon">⚠</div><h3>Σφάλμα</h3><p>${escHtml(e.message)}</p></div>`;
  }
}

function renderUserGrid() {
  const grid = document.getElementById('userGrid');
  const search = (document.getElementById('listSearch').value || '').toLowerCase();
  const genre  = (document.getElementById('listGenre').value  || '').toLowerCase();

  let movies = state.userMovies;
  if (search) movies = movies.filter(m => (m.title || '').toLowerCase().includes(search));
  if (genre)  movies = movies.filter(m => (m.genre || '').toLowerCase().includes(genre));

  grid.innerHTML = '';
  if (!movies.length) {
    grid.innerHTML = `
      <div class="empty-state">
        <div class="empty-icon">📋</div>
        <h3>Η λίστα είναι άδεια</h3>
        <p>Πρόσθεσε ταινίες από το Αρχείο ή κάνε Sync από Google Sheets.</p>
      </div>`;
  } else {
    movies.forEach(m => grid.appendChild(renderMovieCard(m, true)));
  }
}

async function deleteUserMovie(docId, cardEl) {
  if (!confirm('Να διαγραφεί αυτή η εγγραφή;')) return;
  try {
    await api(`/api/user/movies/${encodeURIComponent(docId)}`, { method: 'DELETE' });
    cardEl.remove();
    state.userMovies = state.userMovies.filter(m => m.id !== docId);
    toast('Η εγγραφή διαγράφηκε.', 'info');
  } catch (e) {
    toast('Σφάλμα διαγραφής: ' + e.message, 'error');
  }
}

/* ==============================================
   Sync from Sheets
============================================== */
document.getElementById('syncSheetsBtn').addEventListener('click', async () => {
  const key = prompt('Εισάγετε SYNC_API_KEY:');
  if (key === null) return;
  try {
    const res = await api('/api/user/sync', {
      method: 'POST',
      headers: { 'Authorization': `Bearer ${key}` },
      body: JSON.stringify({ sheet: '', rows: [] }),
    });
    toast(`Sync ολοκληρώθηκε: ${res.synced} εγγραφές`, 'success');
    loadUserMovies();
  } catch (e) {
    toast('Σφάλμα sync: ' + e.message, 'error');
  }
});

/* ==============================================
   Scraping panel
============================================== */
document.getElementById('adminBtn').addEventListener('click', () => {
  document.getElementById('adminOverlay').classList.remove('hidden');
  refreshScrapeStatus();
});
document.getElementById('adminClose').addEventListener('click', () => {
  document.getElementById('adminOverlay').classList.add('hidden');
});

document.getElementById('fullRescrapeToggle').addEventListener('click', function () {
  state.fullRescrape = !state.fullRescrape;
  this.classList.toggle('on', state.fullRescrape);
});

document.getElementById('startScrapeBtn').addEventListener('click', async () => {
  const key  = document.getElementById('scrapeKey').value.trim();
  const mode = document.getElementById('scrapeMode').value;
  setAdminStatus('Εκκίνηση scraping…', 'info');
  try {
    const res = await api('/api/scrape/start', {
      method: 'POST',
      body: JSON.stringify({ api_key: key, mode, full_rescrape: state.fullRescrape }),
    });
    state.scrapeId = res.scrape_id;
    toast(`Scraping ξεκίνησε! ID: ${res.scrape_id}`, 'success');
    startScrapePolling();
  } catch (e) {
    setAdminStatus('Σφάλμα: ' + e.message, 'error');
  }
});

document.getElementById('refreshStatusBtn').addEventListener('click', refreshScrapeStatus);

async function refreshScrapeStatus() {
  try {
    const qs  = state.scrapeId ? `?scrape_id=${state.scrapeId}` : '';
    const job = await api('/api/scrape/status' + qs);
    updateScrapeUI(job);
  } catch (e) {
    setAdminStatus('Δεν βρέθηκε ενεργό job.', 'info');
  }
}

function updateScrapeUI(job) {
  if (!job) return;
  const pct = job.total ? Math.round((job.done / job.total) * 100) : 0;
  document.getElementById('scrapeProgressWrap').style.display = '';
  document.getElementById('scrapeProgressBar').style.width = pct + '%';
  document.getElementById('scrapeProgressLabel').textContent =
    `${job.done} / ${job.total} ταινίες — ${job.status} — σφάλματα: ${job.errors || 0}`;

  if (job.status === 'completed') {
    setAdminStatus(`✓ Ολοκληρώθηκε: ${job.done} ταινίες αποθηκεύτηκαν.`, 'success');
    stopScrapePolling();
  } else if (job.status === 'error') {
    setAdminStatus(`✗ Σφάλμα: ${job.error_message || 'Άγνωστο'}`, 'error');
    stopScrapePolling();
  } else {
    setAdminStatus(`Σε εξέλιξη… (${pct}%)`, 'info');
  }
}

function startScrapePolling() {
  stopScrapePolling();
  state.scrapeInterval = setInterval(refreshScrapeStatus, 3000);
}

function stopScrapePolling() {
  if (state.scrapeInterval) {
    clearInterval(state.scrapeInterval);
    state.scrapeInterval = null;
  }
}

function setAdminStatus(msg, type) {
  const el = document.getElementById('adminStatus');
  el.textContent = msg;
  el.className = `admin-status ${type}`;
  el.classList.remove('hidden');
}

/* ==============================================
   Filter & toolbar listeners
============================================== */
let searchDebounce = null;
document.getElementById('searchInput').addEventListener('input', (e) => {
  clearTimeout(searchDebounce);
  searchDebounce = setTimeout(() => {
    state.filters.q = e.target.value.trim();
    state.page = 1;
    loadMovies();
  }, 300);
});

function addFilterListener(id, key) {
  document.getElementById(id)?.addEventListener('change', (e) => {
    state.filters[key] = e.target.value;
    state.page = 1;
    loadMovies();
  });
}
addFilterListener('yearFrom',     'yearFrom');
addFilterListener('yearTo',       'yearTo');
addFilterListener('imdbMin',      'imdbMin');
addFilterListener('imdbMax',      'imdbMax');
addFilterListener('countrySelect','country');
addFilterListener('genreSelect',  'genre');
addFilterListener('durMin',       'durMin');
addFilterListener('durMax',       'durMax');

document.getElementById('sortBy').addEventListener('change', (e) => {
  state.sortBy = e.target.value;
  state.page = 1;
  loadMovies();
});

document.getElementById('sortDirBtn').addEventListener('click', function () {
  state.sortDir = state.sortDir === 'desc' ? 'asc' : 'desc';
  this.textContent = state.sortDir === 'desc' ? '↓' : '↑';
  state.page = 1;
  loadMovies();
});

document.getElementById('perPage').addEventListener('change', (e) => {
  state.perPage = parseInt(e.target.value);
  state.page = 1;
  loadMovies();
});

document.getElementById('pageSelect').addEventListener('change', (e) => {
  state.page = parseInt(e.target.value);
  loadMovies();
  window.scrollTo({ top: 0, behavior: 'smooth' });
});

document.getElementById('randomBtn').addEventListener('click', loadRandom);

document.getElementById('clearFiltersBtn').addEventListener('click', () => {
  state.filters = {};
  state.page = 1;
  ['searchInput','yearFrom','yearTo','imdbMin','imdbMax','durMin','durMax'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.value = '';
  });
  const cs = document.getElementById('countrySelect');
  const gs = document.getElementById('genreSelect');
  if (cs) cs.value = '';
  if (gs) gs.value = '';
  loadMovies();
});

/* ==============================================
   Tab navigation
============================================== */
document.querySelectorAll('.nav-tab').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.nav-tab').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    state.tab = btn.dataset.tab;

    document.getElementById('archiveTab').style.display = state.tab === 'archive' ? '' : 'none';
    document.getElementById('mylistTab').classList.toggle('active', state.tab === 'mylist');

    if (state.tab === 'mylist') loadUserMovies();
  });
});

/* ==============================================
   My List sub-tabs
============================================== */
document.querySelectorAll('.list-subtab').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.list-subtab').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    state.listType = btn.dataset.listtype;
    loadUserMovies();
  });
});

document.getElementById('listSearch').addEventListener('input', () => renderUserGrid());
document.getElementById('listGenre').addEventListener('change', () => renderUserGrid());

/* ==============================================
   Modal close handlers
============================================== */
document.getElementById('modalClose').addEventListener('click', closeModal);
document.getElementById('modalTopbarClose').addEventListener('click', closeModal);
document.getElementById('movieModal').addEventListener('click', (e) => {
  if (e.target === e.currentTarget) closeModal();
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    closeModal();
    document.getElementById('adminOverlay').classList.add('hidden');
    document.getElementById('addToListMenu').classList.remove('open');
  }
  if (e.key === 'ArrowLeft' && !document.getElementById('movieModal').classList.contains('hidden')) {
    document.getElementById('modalPrevBtn').click();
  }
  if (e.key === 'ArrowRight' && !document.getElementById('movieModal').classList.contains('hidden')) {
    document.getElementById('modalNextBtn').click();
  }
});
document.addEventListener('click', (e) => {
  if (!e.target.closest('.dropdown-wrap')) {
    document.getElementById('addToListMenu').classList.remove('open');
  }
});

/* ==============================================
   Init
============================================== */
(async function init() {
  await loadFiltersMeta();
  loadTotalCount();
  await loadMovies();
})();
