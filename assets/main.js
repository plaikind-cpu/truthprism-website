// Mobile nav toggle
const burger = document.querySelector('.nav-burger');
const navLinks = document.querySelector('.nav-links');

if (burger && navLinks) {
  burger.addEventListener('click', () => {
    const open = navLinks.style.display === 'flex';
    navLinks.style.display = open ? '' : 'flex';
    navLinks.style.flexDirection = 'column';
    navLinks.style.position = 'absolute';
    navLinks.style.top = '64px';
    navLinks.style.left = '0';
    navLinks.style.right = '0';
    navLinks.style.background = 'rgba(7,9,15,0.97)';
    navLinks.style.padding = '24px 36px';
    navLinks.style.gap = '20px';
    navLinks.style.borderBottom = '1px solid rgba(255,255,255,0.08)';
  });
}

// Animate score bars on scroll (Get Started page)
function animateBars() {
  const fills = document.querySelectorAll('.score-fill');
  fills.forEach(fill => {
    const target = fill.dataset.width;
    if (target) fill.style.width = target + '%';
  });
}

const observer = new IntersectionObserver(entries => {
  entries.forEach(e => {
    if (e.isIntersecting) animateBars();
  });
}, { threshold: 0.3 });

const demo = document.querySelector('.score-demo');
if (demo) observer.observe(demo);
