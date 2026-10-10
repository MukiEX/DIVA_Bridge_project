# How AnySlider.dll in this bundle was built

* Source: https://github.com/samnyan/AnySlider, commit d1095e4 (2026-10-08,
  "feat: remap all controllers when vid_pids is empty"), with submodules
  JoyShockLibrary 023dfb6 and hidapi a6a622f. Shared-memory (MMIO) ABI v2.0.
* Toolchain: llvm-mingw 20240619 (clang 18, x86_64-w64-mingw32, UCRT),
  instead of the project's MSVC build. Microsoft Detours 4.0.1 compiled from
  https://github.com/microsoft/Detours source (same version AnySlider ships).
* AnySlider's own source files are unchanged. The only change is to the
  bundled Detours header (detours-mingw.patch):
    - skip an old-compiler LONG_PTR typedef that MinGW already defines;
    - add DetourAttach/DetourDetach overloads that cast function pointers to
      PVOID (MSVC does this implicitly, clang does not).
* Flags: -std=c++20 -fexperimental-library (std::jthread) -fms-extensions
  -D_USE_MATH_DEFINES -DNDEBUG -DUNICODE; linked statically against libc++,
  plus setupapi, hid, winmm, dinput8, dxguid.
* Header-name case shims for Linux (Windows.h -> windows.h, Psapi.h -> psapi.h).

Checked: loads under Wine, exports Init/OnFrame, reads config.toml, and outside
Mega Mix+ refuses to hook (signatures not found, image size check) without
crashing. Its in-game hooks could not be exercised without the game itself.
