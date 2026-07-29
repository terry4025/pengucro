AOS.init();
// ScrollOut();

// Initialize Lenis (Smooth Scroll)
const lenis = new Lenis({
    autoRaf: true,
});

// 패럴랙스 효과 함수
// function applyParallaxEffect() {
//     // 모든 .parallax 요소를 가져옴
//     const parallaxElements = document.querySelectorAll('.parallax');

//     parallaxElements.forEach((element) => {
//         // 요소의 화면상 위치를 가져옴
//         const rect = element.getBoundingClientRect();
//         const windowHeight = window.innerHeight;

//         // 화면에 나타났을 때부터 사라질 때까지 진행 비율 계산
//         const progress = Math.min(Math.max((windowHeight - rect.top) / (windowHeight + rect.height), 0), 1);

//         // translateY 값을 0에서 300px으로 조정
//         const translateX = -20 * progress; // 40 ~ -40
//         element.style.transform = `translateY(${translateX}%)`;
//     });
// }

// // 스크롤 이벤트와 초기 실행
// window.addEventListener('scroll', applyParallaxEffect);
// window.addEventListener('load', applyParallaxEffect);


// NAV
document.addEventListener("DOMContentLoaded", () => {
    const btnMenu = document.getElementById("btnMenu");
    const headerNav = document.getElementById("headerNav");

    btnMenu.addEventListener("click", () => {
        headerNav.classList.toggle("active");
    });
});

// Section OVERLAP
gsap.registerPlugin(ScrollTrigger);

gsap.utils.toArray('.scrollOver').forEach(section => {
  gsap.fromTo(section, 
    { y: '30vh' },
    {
      y: '0vh',
      ease: "none",
      scrollTrigger: {
        trigger: section,
        start: "top bottom", // 요소가 화면 하단에 나타날 때 시작
        end: "top top", // 요소가 화면 상단에 닿을 때 끝
        scrub: true // 스크롤 위치에 따라 애니메이션 진행
      }
    }
  );
});

gsap.utils.toArray('.parallax').forEach(img => {
    gsap.fromTo(img, 
        { yPercent: 0 },
        {
        yPercent: -20,
        ease: "none",
        scrollTrigger: {
            trigger: img,
            start: "top bottom", // 이미지가 화면 하단에 나타날 때 시작
            end: "bottom top", // 이미지가 화면 상단에서 완전히 사라질 때 끝
            scrub: true, // 스크롤 위치에 따라 애니메이션 진행
            toggleActions: "play none none reverse" // 스크롤 방향에 따라 애니메이션 제어
        }
        }
    );
});

//로고 분기처리
if($(window).width() > 768) {
    $('.logo_footer').attr('src', 'img/logo_footer.png');
} else {
    $('.logo_footer').attr('src', 'img/logo_footer_m.png');
}


// MODAL
function openModal(id){
    const modal = document.getElementById("modal");
    const modalContent = document.getElementById(id);

    closeModal();
    modal.classList.add('active');
    modalContent.classList.add('active');
    //$('.scrollbar-outer').scrollbar();
    // preventDefault();

};

//Modal close
function closeModal(){
    document.getElementById('modal').classList.remove('active');
    let modal = document.querySelectorAll('.modalContent');
    modal.forEach(function(e){
        e.classList.remove('active');
    });
};

function loading_bar(){    
    $('#mask').css({display: 'flex'});
    $('#mask').fadeTo("slow",0.6);
};