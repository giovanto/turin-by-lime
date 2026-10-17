// Hand-written copy for the story. Edit freely: build.py never touches this file.
// Photo and video captions live in data/moments.csv.
window.TEXT = {
  intro: {
    kicker: 'Utopian Hours · Turin · 16–17 Oct 2026',
    title: 'Turin by <span class="lime">Lime</span>',
    body: 'Three Lime rides between my hotel and the conference, through the eyes of someone used to cycling in Amsterdam. Where it felt off, and what OpenStreetMap says is there.',
  },
  rides: {
    L1: { title: 'Hotel → conference', note: '' },
    L2: { title: 'Hotel → conference, avoiding the centre', note: 'This time I took a detour to stay out of the pedestrian streets of the historic centre. It worked, but there were still plenty of tram tracks, mixed traffic and cobbles. Very annoying to ride.' },
    L3: { title: 'Back to the hotel, another way', note: '' },
  },
  pedestrian: {
    title: 'Into the pedestrian streets',
    body: 'The navigation kept sending me into pedestrian areas. Bikes are allowed there, a firefighter confirmed it, but riding between people on foot felt unpleasant, and risky for them more than for me.',
  },
  trams: {
    title: 'Tram tracks, everywhere',
    body: 'Under 30° is where a wheel can drop into the rail.',
  },
  numbers: { title: 'Where the rides ran', note: 'Each stretch takes the type of the nearest OSM way within 12 m. GPS wobbles and OSM isn’t the street, so read it as roughly right.' },
  outro: {
    title: 'How I made this',
    body: 'Lime doesn’t let you export your rides, so the track comes from my phone’s location history: a point every 30 to 40 seconds (the white dots), cut by the times on my Lime receipts and matched to the streets. Photos are placed by their own time and GPS.',
    credits: 'Stops with <a href="https://movingpandas.org/">MovingPandas</a> by Anita Graser · map matching with <a href="https://project-osrm.org/">OSRM</a> · street data © <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors · map with MapLibre and deck.gl, basemap <a href="https://openfreemap.org/">OpenFreeMap</a>',
    repo: 'https://github.com/giovanto/turin-by-lime',
  },
};
