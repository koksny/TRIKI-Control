# TRIKI Control Android

Natywny port Android do sterowania grami dotykowymi kapslem TRIKI. Jest wydawany razem z desktopowym TRIKI Control 1.2.0.

## Status

- wersja: `1.2.0`
- minimalny system: Android 8.0 (API 26)
- bez roota, ADB, Shizuku i ukrytych API
- BLE działa jako usługa pierwszoplanowa z widocznym powiadomieniem
- dotyk w innych aplikacjach jest wysyłany przez `AccessibilityService`
- usługa nie pobiera zawartości okien i nie odczytuje tekstu ani ekranu
- uruchomienie i render zweryfikowane na Androidzie 8.0 (API 26) oraz Androidzie 15 (API 35)
- tap, ciągły joystick i przytrzymanie zweryfikowane na API 26 i 35, także w orientacji poziomej
- test kontraktu WebView sprawdza otwieranie i zapis mapowania, reakcje kapsla oraz przejście do listy aplikacji; przechodzi na API 26 i 35

Debugowy wariant używa pakietu `com.koksny.trikicontrol.debug` i nazwy `TRIKI Control Test`, więc może być zainstalowany obok normalnego wydania. Wariant release na liście aplikacji nazywa się `TRIKI Control`.

Wariant `qa` zawiera odbiornik testowy chroniony systemowym uprawnieniem `android.permission.DUMP`. Służy wyłącznie do testów ADB i nie występuje w APK przekazywanym użytkownikowi ani w wariancie `release`.

## Instalacja

1. Pobierz z GitHub Releases plik `TRIKI-Control-Android-1.2.0.apk` i otwórz go.
2. Android poprosi o zgodę na instalację z tego źródła.
3. W aplikacji przeczytaj komunikat o sterowaniu dotykiem, zaznacz zgodę i otwórz ustawienia ułatwień dostępu.
4. Na Androidzie 13 lub nowszym system może pokazać `Ustawienie z ograniczeniami`. Wróć do aplikacji, wybierz `Otwórz listę Wszystkie aplikacje`, wskaż TRIKI Control, otwórz menu z trzema kropkami i wybierz `Zezwól na ustawienia z ograniczeniami`.
5. Włącz usługę `TRIKI Control: sterowanie dotykiem` i wróć do aplikacji.
6. Wybierz `Połącz kapsel`, naciśnij fizyczny przycisk kapsla i zaakceptuj uprawnienia Bluetooth.
7. Po połączeniu przytrzymaj `test diody`. Dioda kapsla powinna świecić tylko podczas trzymania przycisku.
8. W zakładce `Pozycje` ustaw środek i promień wirtualnego joysticka oraz pozycje przycisków ataku, użycia i biegu. Wartości są procentami aktualnego ekranu gry.
9. Użyj testów z pięciosekundowym opóźnieniem. Po naciśnięciu testu przełącz się do gry i sprawdź każdą akcję osobno.
10. Włącz `Sterowanie w grze` dopiero po poprawnym ustawieniu współrzędnych.

Każdy z sześciu rozpoznawanych ruchów można przypisać do dowolnego wyjścia dotykowego albo wyłączyć. Dotknij wiersza w zakładce `Mapowanie`, wybierz wyjście i wróć do testów gestów.
Wyjścia analogowe obejmują lewo, prawo, górę i dół i służą do sterowania osią w grach.
Do przewijanych list i stron służą osobne wyjścia `Przewiń w górę` i `Przewiń w dół`. Wysyłają kolejne pełne gesty swipe przez cały czas trwania ruchu kapsla; wyjścia joysticka pozostają gestami przytrzymania przeznaczonymi do gier.

Sterowanie jest automatycznie wyłączane po rozłączeniu BLE lub wyłączeniu usługi ułatwień dostępu. Kapsel można rozłączyć w aplikacji albo akcją `Rozłącz` w stałym powiadomieniu.

## Mapowanie Game

| Ruch TRIKI | Android |
| --- | --- |
| obrót w lewo / prawo | ciągłe przeciągnięcie wirtualnego joysticka w osi X |
| przechylenie i przytrzymanie | ciągłe przeciągnięcie joysticka do góry |
| stempel | dotknięcie pozycji `Atak` |
| płaskie przesunięcie po stole | dotknięcie pozycji `Użyj` |
| odwrócenie kapsla | przytrzymanie pozycji `Bieg` |

Siła wychylenia joysticka zależy od szybkości obrotu lub przechylenia. Dotyk jest kontynuowany segmentami `GestureDescription.StrokeDescription`, dzięki czemu zachowuje się jak przytrzymany analog, a nie seria pojedynczych naciśnięć.

## Co przetestować

- wykrywanie i połączenie z `Triki 308531776`
- odbiór próbek po około 1,2 s kalibracji
- działanie testu diody
- lewo, prawo, górę i dół w grze z ekranowym joystickiem
- zwolnienie joysticka natychmiast po zatrzymaniu ruchu
- atak, użycie oraz przytrzymanie biegu
- pracę po przejściu aplikacji do tła i po obróceniu gry do poziomu
- rozłączenie z aplikacji i z powiadomienia
- brak dotyku po utracie Bluetooth

Niektóre gry mogą ignorować gesty generowane przez usługę ułatwień dostępu albo blokować je w ramach ochrony przed automatyzacją. To ograniczenie konkretnej gry, nie obejście wymagające roota.

## Budowanie

Projekt wymaga JDK 17, Android SDK Platform 35 i Build Tools 35.0.0. Lokalna ścieżka SDK znajduje się w ignorowanym `local.properties`.

```bash
./gradlew testDebugUnitTest lintDebug assembleDebug assembleRelease assembleQa assembleDebugAndroidTest
```

`assembleRelease` tworzy podpisany APK, gdy w ignorowanym pliku `signing.properties` znajdują się `storeFile`, `storePassword`, `keyAlias` i `keyPassword`. Bez tego pliku Gradle pozostawia release niepodpisany.

Test kontraktu UI można uruchomić na podłączonym emulatorze lub telefonie:

```bash
./gradlew connectedDebugAndroidTest
```

APK powstaje w:

```text
app/build/outputs/apk/debug/app-debug.apk
```

## Architektura

- `MainActivity` - zabezpieczony lokalny WebView i most JavaScript
- `TrikiControlService` - foreground service, natywne BLE GATT/NUS i routing ruchu
- `MotionFrameParser` - strumieniowy parser 14-bajtowych ramek `0x22`
- `TrikiMotionEngine` - kalibracja, twist, tilt, stamp, flip i scrub
- `TrikiAccessibilityService` - globalne tapy i kontynuowane gesty joysticka

Oficjalne mechanizmy użyte przez port:

- [Android BLE permissions](https://developer.android.com/develop/connectivity/bluetooth/bt-permissions)
- [Connect to a GATT server](https://developer.android.com/develop/connectivity/bluetooth/ble/connect-gatt-server)
- [Accessibility services](https://developer.android.com/guide/topics/ui/accessibility/service)
- [GestureDescription](https://developer.android.com/reference/android/accessibilityservice/GestureDescription)
- [Android restricted settings](https://support.google.com/android/answer/12623953)

APK publikowane w GitHub Releases musi być podpisane tym samym kluczem przy każdej aktualizacji. Publikacja w Google Play wymaga dodatkowej deklaracji i zatwierdzenia użycia `AccessibilityService`.
