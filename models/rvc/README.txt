Thư mục model RVC cục bộ của VietDub Studio.

Model của bạn
--------------

RVC runtime không kèm hoặc tự tải model giọng. Đặt các model `.pth` mà bạn
có quyền sử dụng vào thư mục này, ví dụ:

  female_voice.pth
  female_voice.index    (tùy chọn nhưng nên cùng tên với file .pth)

VietDub Studio tự liệt kê các file `.pth` tại đây. Nếu có `.index` cùng tên,
app sẽ dùng index đó khi chuyển giọng. Chỉ thêm model có nguồn gốc và giấy
phép bạn đã kiểm tra.

Model quyết định màu giọng; preset Sáng, trẻ/Sáng rõ/Mềm chỉ điều chỉnh F0 nhẹ
qua RMVPE/RVC để giữ ngữ điệu tự nhiên. Các file `.pth` và `.index` là tài sản
cục bộ, bị bỏ qua khỏi Git/source ZIP.
