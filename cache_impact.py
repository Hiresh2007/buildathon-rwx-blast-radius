"""Carry a shared cache's observed consumers through changes to its producer."""


class CacheConsumers:
    def __init__(self, spans, index):
        self.by_path = {}
        fields = {}
        for path, ranges in spans.items():
            targets = []
            for start, end, symbol, consumer_file in ranges:
                key = (symbol, consumer_file)
                if key not in fields:
                    fields[key] = self._consumers(symbol, consumer_file, index)
                targets.append((start, end, fields[key]))
            self.by_path[path] = targets

    @staticmethod
    def _consumers(symbol, path, index):
        symbols = index.symbols
        bits = symbols.raw.get(symbol, 0) | symbols.copies.get(symbol, 0)
        for mask in symbols.keys.get(symbol, {}).values():
            bits |= mask
        # A module can fetch the cached value during collection, then use it
        # throughout a test without touching the table again.
        imports = set(symbols.initialized.get(symbol, ()))
        for modules in symbols.initialized_keys.get(symbol, {}).values():
            imports.update(modules)
        for module in imports:
            bits |= symbols.modules.get(module, index.all_bits)
        cold = index.isolated.get(path, index.all_bits) | index.unknown
        if not bits or symbols.derived.get(symbol):
            # Decorator wrappers and uninstrumented/derived tables have no
            # reliable per-test lookup mask. Use their consumer module instead.
            bits |= cold | index.reads.get(path, 0)
            for mask in index.files.get(path, {}).values():
                bits |= mask
        return bits | ((index.incomplete | symbols.incomplete) & cold)

    def floor(self, path, replaced, inserted):
        bits = 0
        for start, end, mask in self.by_path.get(path, ()):
            if any(start <= n <= end for n in replaced) or any(start - 1 <= n <= end for n in inserted):
                bits |= mask
        return bits
