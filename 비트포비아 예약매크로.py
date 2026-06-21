from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support.ui import Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import NoAlertPresentException, UnexpectedAlertPresentException, NoSuchElementException
import keyboard
import time

# 브라우저 설정
chrome_options = Options()
chrome_options.add_experimental_option("detach", True)
chrome_options.add_argument("--disable-extensions")
chrome_options.add_argument("--proxy-server='direct://'")
chrome_options.add_argument("--proxy-bypass-list=*")
chrome_options.add_argument("--start-maximized")
chrome_options.add_argument('--no-sandbox')

# 드라이버 설정
driver_reservation = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=chrome_options)
wait_reservation = WebDriverWait(driver_reservation, 8)  # Shorten wait time for faster refresh
reservation_url = ('https://xdungeon.net/layout/res/home.php?go=rev.main&s_zizum=9&rev_days=2026-01-24') # zizum 2 강남던전 / 3 홍대던전 / 1 던전101 / 5 홍대던전 III / 4 강남던전 II / 6 던전루나(강남) / 9 던전스텔라(강남) / 7 서면던전(부산) / 10 서면던전 레드(부산)
driver_reservation.execute_script(f"window.location.href='{reservation_url}'")

abc = True
while abc:
    try:
        print("Checking for the first element...")
        element = driver_reservation.find_element(By.XPATH, '//*[@id="contents"]/div/div[1]/div/div[1]/div[2]/div/div[3]/div[1]/div[2]/ul/li[1]/a') # 뒤에서 3번째 div [x] 테마 / li[x] 테마시간
        print("First element found, clicking it.")
        driver_reservation.execute_script("arguments[0].click();", element)

        # 인원수 선택
        select_element = wait_reservation.until(EC.presence_of_element_located((By.ID, "person")))
        select = Select(select_element)
        select.select_by_value("2") # 명수 2 = 2명 , 3= 3명

        time.sleep(0.1)

        name = wait_reservation.until(EC.presence_of_element_located((By.XPATH, '//*[@id="contents"]/div/div/div[1]/div[2]/div[2]/form/div[1]/table/tbody/tr[2]/td[2]/input'))) #예약자 입력
        name.send_keys('장석환')

        pho = wait_reservation.until(EC.presence_of_element_located((By.XPATH, '//*[@id="contents"]/div/div/div[1]/div[2]/div[2]/form/div[1]/table/tbody/tr[3]/td[2]/input[2]'))) #연락처 1 입력
        pho.send_keys('7532')

        ne = wait_reservation.until(EC.presence_of_element_located((By.XPATH, '//*[@id="contents"]/div/div/div[1]/div[2]/div[2]/form/div[1]/table/tbody/tr[3]/td[2]/input[3]'))) #연락처 2 입력
        ne.send_keys('7760')

        input_element = wait_reservation.until(EC.presence_of_element_located((By.XPATH, '//input[@name="input_captcha"]')))
        actions = ActionChains(driver_reservation)
        input_element.click()
        driver_reservation.execute_script("arguments[0].select();", input_element)

        keyboard.wait("enter")

        #element = wait_reservation.until(EC.presence_of_element_located((By.XPATH, '//input[@name="agree_all"]'))) # 전체동의(마케팅(선택)포함)
        #driver_reservation.execute_script("arguments[0].click();", element)

        element = wait_reservation.until(EC.presence_of_element_located((By.XPATH, '//input[@name="agree_a"]'))) # 개인정보 수집 동의
        driver_reservation.execute_script("arguments[0].click();", element)

        element = wait_reservation.until(EC.presence_of_element_located((By.XPATH, '//input[@name="agree_b"]'))) # 결제관련 주의사항 동의
        driver_reservation.execute_script("arguments[0].click();", element)

        element = wait_reservation.until(EC.presence_of_element_located((By.XPATH, '//*[@id="contents"]/div/div/div[1]/div[2]/div[2]/form/div[3]/a'))) # 예약하기
        driver_reservation.execute_script('arguments[0].click();', element)

        element = wait_reservation.until(EC.presence_of_element_located((By.XPATH, '//*[@id="cancel_popup"]/button'))) #팝업창 확인하기
        driver_reservation.execute_script('arguments[0].click();', element)

        time.sleep(0.1)

        element = wait_reservation.until(EC.presence_of_element_located((By.XPATH, '//*[@id="contents"]/div/div/div/div[2]/div[2]/div[2]/a'))) # 결제하기
        driver_reservation.execute_script('arguments[0].click();', element)

        iframe = wait_reservation.until(EC.presence_of_element_located((By.ID, "__tosspayments_payment-gateway_iframe__")))
        driver_reservation.switch_to.frame(iframe)

        #element = wait_reservation.until(EC.presence_of_element_located((By.XPATH, '//input[@name="all"]'))) # 전체동의
        #driver_reservation.execute_script("arguments[0].click();", element)

        time.sleep(0.3)

        #element = wait_reservation.until(EC.presence_of_element_located((By.XPATH, '//*[@id="AssentChkBtn"]'))) # 다음
        #driver_reservation.execute_script('arguments[0].click();', element)

        select_element2 = wait_reservation.until(EC.presence_of_element_located((By.ID, "vbankBankCode")))
        select2 = Select(select_element2)
        # 1. 은행 이름과 코드 매핑 (보여주신 HTML의 bankCode 기준)
        bank_map = {
            "농협": "11", "국민": "06", "우리": "20", "신한": "26", 
            "기업": "03", "경남": "39", "광주": "34", "대구": "31", 
            "부산": "32", "새마을": "45", "수협": "07", "우체국": "71", "하나": "81"
        }

        # 2. 원하는 은행 이름 설정
        target_bank_name = "국민" 

        try:
            # 해당 bankCode를 가진 a 태그가 나타날 때까지 대기 후 클릭
            bank_code = bank_map.get(target_bank_name, "06") # 기본값 국민(06)
            
            # CSS 선택자를 사용하여 해당 bankCode가 포함된 링크를 찾습니다.
            bank_button = wait_reservation.until(EC.element_to_be_clickable(
                (By.CSS_SELECTOR, f"a[href*='bankCode={bank_code}']")
            ))
            
            # 일반 클릭이 안될 경우를 대비해 자바스크립트로 클릭
            driver_reservation.execute_script("arguments[0].click();", bank_button)
            print(f"[{target_bank_name}] 은행 선택 완료 (코드: {bank_code})")
            
        except Exception as e:
            print(f"은행 리스트에서 [{target_bank_name}]을 찾을 수 없습니다: {e}")

            checkbox = driver_reservation.find_element(By.ID, 'vbankCashReceiptView')
            if checkbox.is_selected():
                checkbox.click()
        

        phone = wait_reservation.until(EC.presence_of_element_located((By.XPATH, '//input[@aria-label="휴대폰번호"]')))
        phone.send_keys('01075327760')

        #element = wait_reservation.until(EC.presence_of_element_located((By.XPATH, '//*[@id="__next"]/div/main/div/div[2]/form/div[2]/button'))) # 다음
        #driver_reservation.execute_script('arguments[0].click();', element)

        #element = wait_reservation.until(EC.presence_of_element_located((By.XPATH, '//*[@id="payDoneBtn"]'))) # 결제하기
        #driver_reservation.execute_script('arguments[0].click();', element)

        print("Reservation successful!")

        abc = False

    except NoSuchElementException:
        print("First element not found, refreshing the page.")
        #driver_reservation.execute_script(f"window.location.href='{reservation_url}'")
        driver_reservation.execute_script("location.reload(true);")


    except UnexpectedAlertPresentException:
        try:
            alert = driver_reservation.switch_to.alert
            print(f"Alert detected: {alert.text}")
            alert.accept()
            print("Alert accepted. Retrying...")
            driver_reservation.execute_script(f"window.location.href='{reservation_url}'")
            continue
        except NoAlertPresentException:
            pass

    except Exception as e:
        # Handle other exceptions and retry the reservation process
        print(f"오류가 발생했습니다: {e}. 다시 시도합니다...")
        driver_reservation.execute_script(f"window.location.href='{reservation_url}'")
        continue
