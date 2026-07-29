// SELECT 대체
document.addEventListener("DOMContentLoaded", () => {
    const customSelects = document.querySelectorAll(".custom-select");

    customSelects.forEach(customSelect => {
        const trigger = customSelect.querySelector(".select-trigger");
        const options = customSelect.querySelector(".select-options");
        const optionItems = options.querySelectorAll("li");

        // 드롭다운 토글
        trigger.addEventListener("click", (e) => {
            // 다른 드롭다운 닫기
            document.querySelectorAll(".select-options").forEach(opt => {
                if (opt !== options) opt.classList.remove("open");
            });

            // 현재 드롭다운 토글
            options.classList.toggle("open");
        });

        // 옵션 클릭 처리
        optionItems.forEach(option => {
            option.addEventListener("click", () => {
                const value = option.getAttribute("data-value");
                const text = option.textContent;

                // 선택된 값 표시
                trigger.textContent = text;
                trigger.setAttribute("data-value", value);

                // 드롭다운 닫기
                options.classList.remove("open");
            });
        });
    });

    // 드롭다운 외부 클릭 시 닫기
    document.addEventListener("click", (e) => {
        customSelects.forEach(customSelect => {
            if (!customSelect.contains(e.target)) {
                const options = customSelect.querySelector(".select-options");
                options.classList.remove("open");
            }
        });
    });
});

// DATEPICKER
// document.addEventListener("DOMContentLoaded", () => {
//     const datepickerInput = document.querySelector(".datepicker");
//     const datepickerUI = document.querySelector(".datepicker-ui");
//     const monthYearDisplay = document.querySelector(".current-month-year");
//     const prevMonthBtn = document.querySelector(".prev-month");
//     const nextMonthBtn = document.querySelector(".next-month");
//     const calendarBody = document.querySelector(".datepicker-calendar tbody");

//     let currentDate = new Date();

//     // 포맷팅 함수 (yyyy-MM-dd)
//     function formatDate(date) {
//         const year = date.getFullYear();
//         const month = String(date.getMonth() + 1).padStart(2, "0");
//         const day = String(date.getDate()).padStart(2, "0");
//         return `${year}-${month}-${day}`;
//     }

//     // 달력 업데이트
//     function updateCalendar(date) {
//         calendarBody.innerHTML = "";

//         const year = date.getFullYear();
//         const month = date.getMonth();
//         monthYearDisplay.textContent = `${year}년 ${month + 1}월`;

//         const firstDayOfMonth = new Date(year, month, 1).getDay();
//         const daysInMonth = new Date(year, month + 1, 0).getDate();

//         let row = document.createElement("tr");

//         // 이전 달 빈칸
//         for (let i = 0; i < firstDayOfMonth; i++) {
//             const cell = document.createElement("td");
//             row.appendChild(cell);
//         }

//         // 현재 달 날짜
//         for (let day = 1; day <= daysInMonth; day++) {
//             if (row.children.length === 7) {
//                 calendarBody.appendChild(row);
//                 row = document.createElement("tr");
//             }

//             const cell = document.createElement("td");
//             cell.textContent = day;
//             cell.addEventListener("click", () => {
//                 datepickerInput.value = formatDate(new Date(year, month, day));
//                 document.querySelectorAll(".datepicker-calendar td").forEach((td) => td.classList.remove("selected"));
//                 cell.classList.add("selected");
//                 datepickerUI.style.display = "none";
//             });
//             row.appendChild(cell);
//         }

//         // 남은 빈칸
//         while (row.children.length < 7) {
//             const cell = document.createElement("td");
//             row.appendChild(cell);
//         }

//         calendarBody.appendChild(row);
//     }

//     // 초기화
//     updateCalendar(currentDate);

//     // 이벤트 핸들러
//     datepickerInput.addEventListener("click", () => {
//         datepickerUI.style.display = "block";
//     });

//     prevMonthBtn.addEventListener("click", () => {
//         currentDate.setMonth(currentDate.getMonth() - 1);
//         updateCalendar(currentDate);
//     });

//     nextMonthBtn.addEventListener("click", () => {
//         currentDate.setMonth(currentDate.getMonth() + 1);
//         updateCalendar(currentDate);
//     });

//     // 클릭 외부 감지
//     document.addEventListener("click", (e) => {
//         if (!datepickerUI.contains(e.target) && e.target !== datepickerInput) {
//             datepickerUI.style.display = "none";
//         }
//     });
// });