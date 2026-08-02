#include <Arduino.h>
#include <WiFi.h>
#include <Firebase_ESP_Client.h>

// Thông tin WiFi
#define WIFI_SSID ""
#define WIFI_PASSWORD ""

// Thông tin Firebase
#define API_KEY ""
#define DATABASE_URL "" 

FirebaseData fbdo;
FirebaseAuth auth;
FirebaseConfig config;
bool signupOK = false;

// Các mảng chứa giá trị cơ sở trích xuất từ dữ liệu chuẩn
// Thứ tự từ Node 1 đến Node 5
float baseAirHum[5]   = {80.0, 72.37, 82.61, 69.60, 51.51};
float baseAirTemp[5]  = {25.0, 26.00, 25.00, 25.50, 25.60};
float baseSoilHum[5]  = {86.0, 60.00, 53.66, 69.83, 68.42};
float baseSoilTemp[5] = {20.0, 22.00, 22.51, 20.15, 21.00};
int   baseWater[5]    = {6, 8, 5, 4, 6};

void setupWiFi() {
  Serial.print("Đang kết nối WiFi");
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  while (WiFi.status() != WL_CONNECTED) {
    Serial.print(".");
    delay(500);
  }
  Serial.println("\nĐã kết nối WiFi!");
}

void setup() {
  Serial.begin(115200);
  setupWiFi();

  config.api_key = API_KEY;
  config.database_url = DATABASE_URL;

  if (Firebase.signUp(&config, &auth, "", "")) {
    Serial.println("Đăng nhập Firebase thành công");
    signupOK = true;
  } else {
    Serial.printf("%s\n", config.signer.signupError.message.c_str());
  }

  Firebase.begin(&config, &auth);
  Firebase.reconnectWiFi(true);
}

void loop() {
  if (Firebase.ready() && signupOK) {
    // Vòng lặp cập nhật dữ liệu giả lập cho cả 5 node
    for (int i = 0; i < 5; i++) {
      String nodePath = "garden1/node" + String(i + 1);
      
      // Tạo dao động ngẫu nhiên
      float airTemp = baseAirTemp[i] + (random(-10, 11) / 10.0);
      float airHum  = baseAirHum[i]  + (random(-50, 51) / 10.0);
      float soilTemp = baseSoilTemp[i] + (random(-5, 6) / 10.0);
      float soilHum  = baseSoilHum[i]  + (random(-30, 31) / 10.0);
      int water_storage = baseWater[i] + random(-2, 2);

      // Tạo JSON cho riêng từng node
      FirebaseJson nodeJson;
      nodeJson.set("air_humidity", airHum);
      nodeJson.set("air_temperature", airTemp);
      nodeJson.set("soil_humidity", soilHum);
      nodeJson.set("soil_temperature", soilTemp);
      nodeJson.set("water_storage", water_storage);
      
      // THÊM DÒNG NÀY: Bảo Firebase tự điền thời gian Server Timestamp (milisecond)
      nodeJson.set("time/.sv", "timestamp");

      Serial.print("Đang cập nhật: ");
      Serial.println(nodePath);

      // Cập nhật lên Firebase theo đường dẫn của từng Node
      if (Firebase.RTDB.updateNode(&fbdo, nodePath, &nodeJson)) {
        Serial.println(" -> Thành công!");
      } else {
        Serial.println(" -> Lỗi: " + fbdo.errorReason());
      }
    }
    Serial.println("------------------------------------");
  }

  // Chờ 10 giây cho lần cập nhật tiếp theo (nên để 5000 - 10000ms thay vì 1000ms để tránh bị Firebase giới hạn request)
  delay(10000); 
}