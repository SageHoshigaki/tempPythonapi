import av

in_path = ("/Users/maybeeravenn/Desktop/Mp4ToMp3/testvid1.mp4")
out_path = "testaudio.mp3"


in_container = av.open(in_path)

# Open output for writing; mp3 is the container/format here
out_container = av.open(out_path, mode="w", format="mp3")


in_audio = next(s for s in in_container.streams if s.type == "audio")

# Create an MP3 encoder stream in the output container
out_audio = out_container.add_stream("mp3", rate=in_audio.rate or 44100)
out_audio.bit_rate = 192_000

# Ensure channel layout is set (stereo/mono). Fall back safely.
out_audio.layout = in_audio.layout.name if in_audio.layout else "stereo"

# Resampler to make sure frames match what the MP3 encoder expects
resampler = av.audio.resampler.AudioResampler(
    format="s16",
    layout=out_audio.layout,
    rate=out_audio.rate,
)

# Demux packets from the input audio stream, decode to frames, resample, encode to mp3, mux
for packet in in_container.demux(in_audio):
    if packet.dts is None:
        continue

    for frame in packet.decode():
        frame = resampler.resample(frame)


        frames = frame if isinstance(frame, list) else [frame]

        for fr in frames:
            for out_packet in out_audio.encode(fr):
                out_container.mux(out_packet)

# Flush encoder (very important—gets buffered audio written)
for out_packet in out_audio.encode(None):
    out_container.mux(out_packet)

in_container.close()
out_container.close()

print(f"Saved MP3 to: {out_path}")